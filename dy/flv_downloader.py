# -*- coding: utf-8 -*-
"""直播流「下载方式」录制 —— 对应原软件的 rdbtn_下载流。

原软件对这个功能的完整说明（core.exe 字符串堆里的 UI 文本）：

    录制方式：
    录制：使用ffmpeg进行直播录制。
    下载：使用软件下载方式。
    两者区别：
    录制：保存的是ts格式，有播放器可播放。
    下载：保存的是FLV格式，无法播放需要转码。
    为什么会有选项：
    如果选择录制，无法录制，就选下载方式。
    如果选择下载，无法下载，就选录制方式。

所以本模块与 ffmpeg 录制（recorder.py）是**并列的两种录制方式**，
由全局设置「录制方式」选择（原软件 Frm_Main 的 rdbtn_录制流 / rdbtn_下载流）。

实现要点
--------
抖音 flv_pull_url 返回的就是一个完整 FLV 流（自带 FLV 头），
所以「下载」= 顺序 GET 该 URL，把响应体按 tag 边界落盘，得到合法 .flv。

比 ffmpeg 强的地方：
  * ffmpeg 的 HTTP 输入在 CDN 主动断开 / 被限流时会直接退出（任务随之结束）；
    本模块支持断点续传，直播断流后接着写，不会中断录制。
  * 只把「完整 FLV tag」刷盘，任何时刻文件都以完整 tag 收尾，
    续传不会产生半个 tag 的坏帧（否则后半段不可解析）。

FLV 字节布局（据此做 tag 边界对齐）：
    "FLV" | 0x01 | flags | header_size(4, =9)      <- 9 字节
    PreviousTagSize0 (4, =0)
    重复 N 次：
        TagType(1) DataSize(3 BE) Timestamp(3) TimestampExt(1) StreamID(3)
        Data(DataSize)
        PreviousTagSize(4, = DataSize + 11)         <- 一个「块」= 15 + DataSize

只用标准库（urllib / threading / time），与 dy/douyin.py 一致。
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request

# 一个 tag 块的固定开销 = 11 字节 tag 头 + 4 字节 PreviousTagSize
_TAG_HDR = 11
_PREV_SIZE = 4
_FLV_HEADER = 13            # 9 字节 FLV 头 + 4 字节 PreviousTagSize0
_READ_CHUNK = 64 * 1024     # 每次从网络读的字节数
_RECONNECT_MIN = 0.5        # 断线重连退避起点（秒）
_RECONNECT_MAX = 10.0       # 断线重连退避上限（秒）
_MAX_RECONNECT = 200        # 连续重连上限，防止永久挂在一个坏 URL 上


class FlvDownloadError(Exception):
    """逻辑上不可恢复的错误（响应不是 FLV、服务器拒绝等）。"""


class _FlvWriter:
    """把 FLV 字节流按 tag 边界写入文件，保证文件任何时刻都以完整 tag 收尾。"""

    def __init__(self, path, resume=False):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # resume=True 时追加（续传），否则新建/覆盖
        self._f = open(path, "ab" if resume else "wb")
        self.path = path
        self.off = os.path.getsize(path) if resume else 0
        self._f.seek(self.off)
        self._buf = bytearray()
        # 0 = 还要写 13 字节文件头；1 = 按 tag 块循环
        self._state = 0 if self.off == 0 else 1
        self.tags = 0
        self._f.flush()

    # ---------------------------------------------------------------- 解析

    def _flush_tags(self):
        """把缓冲里完整的部分写盘，返回本批发盘字节数。"""
        buf = self._buf
        n = len(buf)
        pos = 0
        written = 0
        while True:
            if self._state == 0:
                # 只在新文件时走这里：先写 13 字节文件头
                if n - pos < _FLV_HEADER:
                    break
                if bytes(buf[pos:pos + 3]) != b"FLV":
                    # 不是 FLV 流（可能是 HTML 错误页 / 403 页面）
                    raise FlvDownloadError(
                        "响应不是 FLV 流（开头 %r）"
                        % bytes(buf[pos:pos + 16]))
                self._f.write(bytes(buf[pos:pos + _FLV_HEADER]))
                written += _FLV_HEADER
                pos += _FLV_HEADER
                self.off += _FLV_HEADER
                self._state = 1
                continue

            # self._state == 1：一个 tag 块
            if n - pos < _TAG_HDR:
                break
            data_size = int.from_bytes(buf[pos + 1:pos + 4], "big")
            block = _TAG_HDR + data_size + _PREV_SIZE
            if n - pos < block:
                break
            self._f.write(bytes(buf[pos:pos + block]))
            written += block
            pos += block
            self.off += block
            self.tags += 1
        if pos:
            del buf[:pos]
        return written

    def feed(self, data):
        """喂入从网络读到的字节，返回本批发盘字节数。"""
        if not data:
            return 0
        self._buf += data
        # 正常直播流单个 tag 只有几 KB；缓冲长期超过 16MB 说明已经错位，
        # 丢掉重新对齐（宁可丢一段也不要写坏文件）
        if len(self._buf) > 16 * 1024 * 1024:
            del self._buf[:]
            self._state = 1
        out = self._flush_tags()
        if out:
            self._f.flush()
        return out

    @property
    def buffered(self):
        """已从网络收到、但还没凑成完整 tag 而缓存在内存里的字节。"""
        return len(self._buf)

    @property
    def consumed(self):
        """已经从网络消费掉的字节数 = 落盘 + 内存缓冲。

        断点续传必须用这个值发 Range：若只发已落盘偏移 writer.off，
        缓冲里那几个字节会被重复下载，tag 永远凑不齐（表现为反复重连、
        文件大小卡住不动）。
        """
        return self.off + len(self._buf)

    def close(self):
        try:
            self._f.flush()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                self._f.close()
            except Exception:  # noqa: BLE001
                pass


class StreamDownloader:
    """单个直播流的「下载」式录制任务。

    生命周期：start() 起后台线程 → is_active()/elapsed()/output_of() 查询 →
    stop() 只置停止标志，由线程自己关文件（保证最后一个 tag 完整写完）。
    """

    def __init__(self, url, path, headers=None, stop_minutes=0,
                 segment_minutes=0, base_name="download", outdir=None,
                 label=""):
        self.url = url
        self.path = path
        self.headers = dict(headers or {})
        self.stop_minutes = stop_minutes or 0
        self.segment_minutes = segment_minutes or 0
        self.label = label
        self._base = base_name
        self._outdir = outdir or (os.path.dirname(path) or ".")
        self._stop = threading.Event()
        self._thread = None
        self._start = 0.0
        self._seg_started = 0.0
        self._seg_index = 1
        self._rotations = 0        # 因服务端不支持续传而轮转文件的次数
        self._last_file = path
        self._written = 0
        self._reconnects = 0
        self._stalled = 0        # 连续零进度（没有新字节）的重连次数
        self.error = ""

    # ------------------------------------------------------------ 查询

    def is_active(self):
        return bool(self._thread and self._thread.is_alive()
                    and not self._stop.is_set())

    def elapsed(self):
        if not self._start:
            return 0
        if self.is_active():
            return int(time.time() - self._start)
        return int(self._elapsed_at_end)

    def output_of(self):
        return self._last_file

    def bytes_written(self):
        return self._written

    def reconnects(self):
        return self._reconnects

    # ------------------------------------------------------------ 控制

    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="flv-%s" % (self.label or self._base),
            daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=6.0):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        return not bool(t and t.is_alive())

    # ------------------------------------------------------------ 文件轮转

    def _new_writer(self, resume=False):
        if self.segment_minutes or self._rotations > 0:
            p = os.path.join(self._outdir, "%s_%03d.flv"
                             % (self._base, self._seg_index))
        else:
            p = self.path
        self._last_file = p
        w = _FlvWriter(p, resume=resume)
        self._seg_started = time.time()
        return w

    # ------------------------------------------------------------ 主循环

    def _run(self):
        backoff = _RECONNECT_MIN
        writer = None
        self._start = time.time()
        self._elapsed_at_end = 0
        try:
            while not self._stop.is_set():
                # ---- 定时停止
                if self.stop_minutes and \
                        time.time() - self._start >= self.stop_minutes * 60:
                    break

                # ---- 分段切换：关掉当前段，下一轮开新文件
                if (writer is not None and self.segment_minutes
                        and self._seg_started
                        and time.time() - self._seg_started
                        >= self.segment_minutes * 60):
                    writer.close()
                    writer = None
                    self._seg_index += 1
                    continue

                # ---- 打开文件（只在第一次和分段/轮转后发生）
                if writer is None:
                    writer = self._new_writer(resume=False)
                    continue

                # ---- 读一个连接
                off_before = writer.consumed
                try:
                    verdict = self._drain(writer, resume_off=(
                        writer.consumed if self._reconnects else 0))
                except FlvDownloadError as e:
                    self.error = str(e)
                    break
                except Exception as e:  # noqa: BLE001
                    if self._stop.is_set():
                        break
                    self.error = str(e)
                    verdict = "error"

                # ---- 零进度检测：连续几次重连都没拿到新字节就结束，
                # 否则遇到「已读完的源」会永远重连下去。
                # 用 consumed 而不是 off：下载大 tag 时字节先进缓冲、
                # 凑齐才落盘，只看 off 会误判成零进度。
                if writer.consumed <= off_before:
                    self._stalled += 1
                else:
                    self._stalled = 0
                if self._stalled >= 3:
                    self.error = "连续 %d 次重连都没有新数据，判定流已结束" \
                                 % self._stalled
                    break

                backoff = _RECONNECT_MIN

                if verdict == "stopped":
                    break

                if verdict == "range_ignored":
                    # 服务端忽略 Range，无法续传。绝不能接着 append
                    # （会把重复数据写进同一个文件），换成新文件继续录。
                    if self._stop.is_set():
                        break
                    writer.close()
                    writer = None
                    self._rotations += 1
                    self._seg_index += 1
                    self._reconnects += 1
                    backoff = _RECONNECT_MIN
                    continue

                # ---- 连接断了（eof/error/segment）：接着这个 writer 续传。
                # 关键：writer 不能重建——_buf 里未落盘的半个 tag 必须留着，
                # 否则重连会永远卡在同一个文件偏移上（数据都来了，
                # 但每次都因为 tag 没凑齐又被丢掉）。
                if self._stop.is_set():
                    break
                self._reconnects += 1
                if self._reconnects >= _MAX_RECONNECT:
                    self.error = "连续重连 %d 次仍失败：%s" % (
                        _MAX_RECONNECT, self.error)
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX)
        finally:
            if writer:
                writer.close()
            self._elapsed_at_end = int(time.time() - self._start) \
                if self._start else 0

    def _drain(self, writer, resume_off=0):
        """开一次 HTTP 连接把流读到底。

        返回 "eof"（流结束）、"stopped"（用户停止）、"segment"（到了分段点）、
        "range_ignored"（服务端不支持断点续传）。
        """
        req = urllib.request.Request(self.url)
        for k, v in self.headers.items():
            req.add_header(k, v)
        if resume_off:
            # 关键：必须真的带 Range，否则服务端会从字节 0 重传
            req.add_header("Range", "bytes=%d-" % resume_off)
        with urllib.request.urlopen(req, timeout=90) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            if code not in (200, 206):
                raise FlvDownloadError("HTTP %s" % code)
            if resume_off and code == 200:
                # 返回 200 而不是 206 → Range 被忽略，别 append
                return "range_ignored"
            while not self._stop.is_set():
                # 分段切分要尽快关连接，否则新文件里混着上一段的尾巴
                if (self.segment_minutes and self._seg_started
                        and time.time() - self._seg_started
                        >= self.segment_minutes * 60):
                    return "segment"
                if self.stop_minutes and \
                        time.time() - self._start >= self.stop_minutes * 60:
                    return "eof"
                chunk = resp.read(_READ_CHUNK)
                if not chunk:
                    return "eof"
                self._written += writer.feed(chunk)
            return "stopped"
