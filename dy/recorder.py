# -*- coding: utf-8 -*-
"""直播录制（ffmpeg）

复用原软件自带的 ffmpeg（JHlib/ffmpeg），也支持系统 PATH。
支持：录制质量选择、分段录制、定时停止、录制内容（视频 / 音频，RecContent 枚举）。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time

from . import config
from .flv_downloader import StreamDownloader
from .models import _safe_name
from .store import log

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 说明：清晰度（原画/超清/高清/标清/流畅）在选流阶段决定——
# 由 douyin.LiveRoom.stream_for(quality) 挑选对应码率的 flv_pull_url，
# 录制端一律 -c copy 直存，不转码（与原软件行为一致，CPU 占用最低）。
# 传进来的 stream_url 已经是目标清晰度的地址，quality 仅用于日志与命名。


def build_record_args(ffmpeg, stream_url, outdir, base, content="视频",
                      segment_minutes=0, stop_minutes=0):
    """拼装 ffmpeg 录制参数。返回 (args, outpath, ext)。

    抽成独立函数便于单测（参数顺序错一点就会录不出文件）。
    一律 -c copy 直存，不转码。

    content 取值与原软件 RecContent 枚举一致：
      "视频"（Video，有视频有声） / "音频"（Audio，无视频有声）
    同时兼容早期写法的 "视频+音频" / "仅视频" / "仅音频"。
    """
    args = [ffmpeg, "-y", "-loglevel", "warning",
            "-rw_timeout", "15000000", "-i", stream_url]

    # ---- 编码参数
    audio_only = content in ("音频", "仅音频")
    video_only = content in ("仅视频",)
    if audio_only:
        ext = ".m4a"
        args += ["-vn", "-c:a", "copy"]
    else:
        ext = ".mp4"
        if video_only:
            args += ["-an"]
        args += ["-c", "copy", "-bsf:a", "aac_adtstoasc"]

    # ---- 输出参数（必须在输出目标之前）
    if stop_minutes and stop_minutes > 0:
        args += ["-t", str(int(stop_minutes * 60))]

    if segment_minutes and segment_minutes > 0:
        pat = os.path.join(outdir, base + "_%03d" + ext)
        args += ["-f", "segment",
                 "-segment_time", str(int(segment_minutes * 60)),
                 "-reset_timestamps", "1",
                 "-segment_format", "mp4",
                 pat]
        outpath = os.path.join(outdir, base + "_001" + ext)
    else:
        outpath = os.path.join(outdir, base + ext)
        args.append(outpath)

    return args, outpath, ext


class Recorder:
    """管理多个主播的录制任务，支持两种「录制方式」。

    录制方式（对应原软件 Frm_Main 的 rdbtn_录制流 / rdbtn_下载流）：
      "录制流"：ffmpeg 录制，输出 ts/mp4（有播放器可直接播放）
      "下载流"：直接下载 FLV 流（StreamDownloader），输出 .flv
    """

    def __init__(self):
        self._procs = {}      # key -> subprocess.Popen   （录制流）
        self._meta = {}       # key -> dict(start, out, timer)
        self._dl = {}         # key -> StreamDownloader    （下载流）
        self._dl_err = {}     # key -> 下载流结束时的错误信息
        self._last_out = {}   # key -> 最近一次输出的文件（停止后仍可查）
        self._last_state = {} # key -> 下载流停止时的状态快照
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 查询

    def _mode_of(self, key):
        with self._lock:
            if key in self._dl:
                return "download"
            if key in self._procs:
                return "ffmpeg"
        return None

    def is_recording(self, key):
        with self._lock:
            return self._raw_recording(key)

    def elapsed(self, key):
        """已录时长（秒）。任务停止后仍返回停止那一刻的时长。"""
        with self._lock:
            d = self._dl.get(key)
            meta = self._meta.get(key)
            last = self._last_state.get(key, {})
            recording = self._raw_recording(key)
        if d is not None:
            return d.elapsed() if recording else int(last.get("last_elapsed", 0))
        if meta is not None:
            if recording:
                return int(time.time() - meta["start"])
            return int(meta.get("last_elapsed", 0))
        return int(last.get("last_elapsed", 0))

    def _raw_recording(self, key):
        """已持有锁的 is_recording 内部版。"""
        p = self._procs.get(key)
        if p is not None:
            return p.poll() is None
        d = self._dl.get(key)
        if d is not None:
            return d.is_active()
        return False

    def active_count(self):
        with self._lock:
            keys = list(self._procs.keys()) + list(self._dl.keys())
        return sum(1 for k in keys if self.is_recording(k))

    def output_of(self, key):
        """最近一次录制的输出文件。任务停止后仍然可以查询。"""
        with self._lock:
            if key in self._last_out:
                return self._last_out[key]
            m2 = self._meta.get(key)
            return m2.get("out") if m2 else None

    def last_error(self, key):
        m = self._mode_of(key)
        if m == "download":
            with self._lock:
                d = self._dl.get(key)
            if d:
                return d.error or ""
        if key in self._dl_err:
            # 下载流刚结束、还没被新的任务顶掉时也能读到
            return self._dl_err.pop(key) or ""
        with self._lock:
            meta = self._meta.get(key)
            path = meta.get("errpath") if meta else None
        if not path or not os.path.isfile(path):
            return ""
        try:
            with open(path, "rb") as f:
                data = f.read()
            return (data or b"").decode("utf-8", "replace")[-600:]
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------ 开始

    def start(self, anchor, stream_url, quality="原画", content="视频",
              segment_minutes=0, stop_minutes=0, mode="录制流",
              headers=None):
        """启动录制。mode = "录制流"（ffmpeg）/ "下载流"（FLV 下载）。"""
        key = anchor.key
        with self._lock:
            if self.is_recording(key):
                return None, "该主播已在录制中"

            outdir = anchor.video_dir
            os.makedirs(outdir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            base = "%s_%s" % (_safe_name(anchor.主播名称 or "主播"), ts)

        # ---------------- 下载流 ----------------
        if mode == "下载流":
            if not stream_url:
                return None, "没有可用的直播流地址"
            path = os.path.join(outdir, base + ".flv")
            d = StreamDownloader(
                stream_url, path,
                headers=headers or {},
                stop_minutes=stop_minutes,
                segment_minutes=segment_minutes,
                base_name=base, outdir=outdir, label=anchor.主播名称)
            with self._lock:
                self._dl[key] = d
            d.start()
            log("启动下载录制: %s [%s] -> %s" % (anchor.主播名称, quality, path))
            return path, ""

        # ---------------- 录制流（ffmpeg） ----------------
        if not config.FFMPEG:
            raise RuntimeError(
                "未找到 ffmpeg。请把 ffmpeg 放到程序目录的 ffmpeg/ 下，"
                "或加入系统 PATH。")
        if not stream_url:
            return None, "没有可用的直播流地址"

        with self._lock:
            args, outpath, _ext = build_record_args(
                config.FFMPEG, stream_url, outdir, base,
                content=content, segment_minutes=segment_minutes,
                stop_minutes=stop_minutes)

            log("启动录制: %s [%s] -> %s" % (anchor.主播名称, quality, outpath))

            # stderr 必须落到文件而不是 PIPE：ffmpeg 长时间运行时会持续
            # 输出进度/重连信息，PIPE 写满 64KB 后 ffmpeg 会阻塞卡死，
            # 表现为「录制中但文件不再增长」。写文件则永不阻塞。
            # 日志单独放运行日志/ffmpeg/，不污染录制目录。
            logdir = os.path.join(config.LOG_DIR, "ffmpeg")
            os.makedirs(logdir, exist_ok=True)
            errpath = os.path.join(logdir, base + ".log")
            errf = open(errpath, "wb")
            try:
                p = subprocess.Popen(
                    args, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=errf,
                    creationflags=CREATE_NO_WINDOW)
            except Exception as e:  # noqa: BLE001
                errf.close()
                log("录制启动失败: %s" % e)
                raise

            self._procs[key] = p
            self._meta[key] = {"start": time.time(), "out": outpath,
                               "timer": stop_minutes, "proc": p,
                               "errpath": errpath, "errf": errf}
        return outpath, ""

    # ------------------------------------------------------------ 停止

    def stop(self, key):
        """停止录制。

        ffmpeg 那路不能直接 terminate —— 那样 mp4 的 moov atom 不会写入，
        文件会变成「moov atom not found」完全无法播放。
        必须先向 ffmpeg 的 stdin 发送 'q' 让它优雅收尾。

        下载流那路只需要置停止标志，StreamDownloader 自己会关文件，
        而且只写完整 tag，所以 .flv 任何时候都能正常解析。
        """
        m = self._mode_of(key)
        if m == "download":
            with self._lock:
                d = self._dl.pop(key, None)
            if not d:
                return False
            # 先记快照再弹表，否则 output_of / elapsed / last_error 都查不到
            with self._lock:
                self._last_out[key] = d.output_of()
                self._last_state[key] = {"last_elapsed": d.elapsed()}
                self._dl_err[key] = d.error or ""
            d.stop()
            log("已停止下载录制: %s" % key)
            return True

        with self._lock:
            p = self._procs.get(key)
            m2 = self._meta.get(key)
            if not p:
                return False
            if m2:
                m2["last_elapsed"] = int(time.time() - m2["start"])
                with self._lock:
                    self._last_out[key] = m2.get("out")
                    self._last_state[key] = {"last_elapsed": m2["last_elapsed"]}
            self._procs.pop(key, None)
            self._meta.pop(key, None)

        if p.poll() is None:
            self._graceful_quit(p)
        self._close_errfile(m2)
        log("已停止录制: %s" % key)
        return True

    @staticmethod
    def _close_errfile(meta):
        """关闭该录制任务对应的 ffmpeg 日志文件句柄。

        日志为空（即录制期间没有任何告警/错误）时直接删除，避免冗余文件。
        """
        if not meta:
            return
        f = meta.pop("errf", None)
        if f is not None:
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass
        path = meta.get("errpath")
        if path and os.path.isfile(path):
            try:
                if os.path.getsize(path) == 0:
                    os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _graceful_quit(p, timeout=20):
        """让 ffmpeg 写完文件尾再退出。"""
        # 1) 发 'q'（ffmpeg 的退出命令，会触发文件收尾）
        try:
            if p.stdin:
                p.stdin.write(b"q")
                p.stdin.flush()
                p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            p.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            pass
        # 2) 兜底：先 terminate（Windows 下等价于 TerminateProcess，会丢尾）
        try:
            p.terminate()
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        log("警告: ffmpeg 未在超时内优雅退出，文件可能缺少收尾")
        return False

    def stop_all(self):
        with self._lock:
            keys = list(self._procs.keys()) + list(self._dl.keys())
        for k in keys:
            self.stop(k)

    # ------------------------------------------------------------ 回收

    def poll_finished(self):
        """返回本轮自行结束（如定时停止 / 流断开）的 key 列表。

        下载流那路：StreamDownloader 跑完自动结束，这里把它从登记表摘掉。
        """
        done = []
        with self._lock:
            for key, p in list(self._procs.items()):
                if p.poll() is not None:
                    m = self._meta.get(key) or {}
                    if m:
                        m["last_elapsed"] = int(time.time() - m["start"])
                    self._close_errfile(m)
                    done.append(key)
                    self._procs.pop(key, None)
            for key, d in list(self._dl.items()):
                if not d.is_active():
                    # 先把错误留住再摘表，否则监控侧拿不到失败原因
                    self._dl_err[key] = d.error or ""
                    done.append(key)
                    self._dl.pop(key, None)
        return done


def probe_stream(stream_url):
    """用 ffprobe 快速探测流是否可用（可选）。"""
    ffprobe = os.path.join(os.path.dirname(config.FFMPEG), "ffprobe.exe")
    if not os.path.isfile(ffprobe):
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=format_name",
             "-of", "default=nw=1", stream_url],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW)
        return r.stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def media_info(path):
    """读取媒体文件信息（时长/分辨率/码率）。"""
    ffprobe = os.path.join(os.path.dirname(config.FFMPEG), "ffprobe.exe")
    if not config.FFMPEG or not os.path.isfile(ffprobe):
        return {}
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration,bit_rate,size:stream=codec_type,width,height,"
             "codec_name",
             "-of", "json", path],
            capture_output=True, text=True, timeout=20,
            creationflags=CREATE_NO_WINDOW)
        import json as _json
        return _json.loads(r.stdout or "{}")
    except Exception:  # noqa: BLE001
        return {}


# 视频转换预设：名称 -> ffmpeg 输出参数（对应原软件 UserC_VideoConve）
CONVERT_PRESETS = {
    "MP4（H.264，通用）": ["-c:v", "libx264", "-preset", "veryfast",
                       "-crf", "23", "-c:a", "aac", "-b:a", "128k"],
    "MP4（无损直拷）": ["-c", "copy"],
    "MKV（H.264）": ["-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "23", "-c:a", "aac"],
    "仅提取音频（MP3）": ["-vn", "-c:a", "libmp3lame", "-q:a", "2"],
    "仅提取音频（M4A）": ["-vn", "-c:a", "aac", "-b:a", "128k"],
    "压缩到 720p": ["-vf", "scale=-2:720", "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "26", "-c:a", "aac"],
    "压缩到 480p": ["-vf", "scale=-2:480", "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "28", "-c:a", "aac"],
}

CONVERT_EXT = {
    "仅提取音频（MP3）": ".mp3",
    "仅提取音频（M4A）": ".m4a",
    "MKV（H.264）": ".mkv",
}


def convert(src, dst, preset="MP4（H.264，通用）", on_line=None):
    """调用 ffmpeg 转换/压缩媒体文件。返回 (成功, 输出信息)。"""
    if not config.FFMPEG:
        return False, "未找到 ffmpeg"
    args = [config.FFMPEG, "-y", "-loglevel", "info", "-i", src]
    args += CONVERT_PRESETS.get(preset, CONVERT_PRESETS["MP4（H.264，通用）"])
    args.append(dst)
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=3600,
                           creationflags=CREATE_NO_WINDOW)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if p.returncode != 0:
        return False, (p.stderr or "")[-800:]
    return True, dst
