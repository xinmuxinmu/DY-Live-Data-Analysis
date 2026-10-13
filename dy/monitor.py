# -*- coding: utf-8 -*-
"""监控主循环

后台线程按间隔轮询主播列表，检测开播/关播/在线人数变化，
按配置触发提醒与录制。
"""
from __future__ import annotations

import threading
import time
import traceback

from .douyin import DouyinError, DouyinLive, human_count
from .notifier import Notifier
from .pusher import Pusher
from .recorder import Recorder
from .store import append_record_event, append_snapshot, log
from .models import snapshot_from_room, primary_ratio


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Monitor:
    def __init__(self, get_anchors, get_config, emit=None):
        """
        get_anchors: () -> list[Anchor]
        get_config:  () -> dict
        emit:        (level, text) -> None   用于回传界面日志
        """
        self._get_anchors = get_anchors
        self._get_config = get_config
        self._emit = emit or (lambda level, text: None)
        self.client = DouyinLive(log=self._log)
        self.recorder = Recorder()
        self.notifier = Notifier(get_config)
        self.pusher = Pusher(get_config)
        self._thread = None
        self._stop = threading.Event()
        self._running = False
        self.cycles = 0
        self.last_run = ""

    # ------------------------------------------------------------ 生命周期

    @property
    def running(self):
        return self._running

    def start(self):
        if self._running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="monitor",
                                        daemon=True)
        self._running = True
        self._thread.start()
        self._log("监控已启动")
        return True

    def stop(self):
        if not self._running:
            return False
        self._stop.set()
        self._running = False
        self.recorder.stop_all()
        self._log("监控已停止")
        return True

    # ------------------------------------------------------------ 主循环

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._cycle()
            except Exception as e:  # noqa: BLE001
                self._log("监控循环异常: %s" % e)
                log(traceback.format_exc())
            self.cycles += 1
            self.last_run = time.strftime("%Y-%m-%d %H:%M:%S")

            # 回收已自行结束的录制
            for key in self.recorder.poll_finished():
                a = self._find(key)
                if a:
                    a.是否正在录制 = False
                    a.录制状态 = "已停止"
                    append_record_event(a, "录制结束", "进程自行结束")
                    if a.是否停止录制提醒:
                        self.notifier.play("停止录制")
                    self.pusher.push("停止录制",
                                     "「%s」录制已结束" % a.主播名称)
                    self._emit("warn", "「%s」录制结束" % a.主播名称)

            interval = max(5, int(self._get_config().get("监控间隔秒", 30)))
            spent = time.time() - t0
            self._stop.wait(max(1, interval - spent))

    def _cycle(self):
        anchors = [a for a in self._get_anchors() if a.是否监控]
        if not anchors:
            return
        for a in anchors:
            if self._stop.is_set():
                return
            self._check_one(a)
            self.client.sleep()

    # ------------------------------------------------------------ 单个主播

    def _check_one(self, a):
        sec = a.主播唯一码
        if not sec:
            sec = DouyinLive.parse_sec_uid(a.主播唯一码) or a.主播唯一码
        if not sec:
            a.错误 = "缺少主播唯一码"
            return

        # ---- 监控时间：对应原软件主列表「监控时间」列，
        # 显示的是「最后一次刷新该直播间的时间」（LiveRecInfo.最后监控时间），
        # 不是已监控时长。每次轮询到该主播都要刷新，无论是否开播。
        a.是否正在监控 = True

        try:
            room, user = self.client.live_of_user(sec)
        except DouyinError as e:
            a.最后监控时间 = _now()
            a.错误 = str(e)
            self._log("「%s」查询失败: %s" % (a.主播名称, e))
            return

        a.最后监控时间 = _now()
        a.错误 = ""
        a.touch()
        was_live = bool(a.是否开播)

        if room is None:
            # 未开播
            a.是否开播 = False
            a.在线人数 = 0
            a.显示在线人数 = "0"
            a.直播标题 = ""
            if was_live:
                self._on_live_end(a)
            return

        # 开播中
        a.是否开播 = True
        a.直播标题 = room.title or a.直播标题
        a.在线人数 = room.user_count
        a.显示在线人数 = room.user_count_str or human_count(room.user_count)
        if room.web_rid:
            a.房间号 = room.web_rid
        if room.nickname:
            a.主播名称 = room.nickname
        if room.display_id:
            a.抖音号 = room.display_id
        if room.follower_count is not None:
            a.粉丝量 = room.follower_count
        if room.like_count is not None:
            a.点赞量 = room.like_count
        if room.live_url:
            a.直播地址 = room.live_url
        try:
            cur = int(room.user_count or 0)
            if cur > int(a.最高在线人数 or 0):
                a.最高在线人数 = cur
        except (TypeError, ValueError):
            pass

        if not was_live:
            self._on_live_start(a, room)

        # 在线人数提醒
        cfg = self._get_config()
        if cfg.get("在线人数提醒"):
            try:
                th = int(cfg.get("在线人数提醒阈值", 10000))
                if room.user_count and int(room.user_count) >= th:
                    self.notifier.play("在线人数")
                    self.pusher.push("在线人数", "「%s」在线人数达到 %s"
                                     % (a.主播名称, a.显示在线人数))
                    self._emit("info", "「%s」在线人数达到 %s"
                               % (a.主播名称, a.显示在线人数))
            except (TypeError, ValueError):
                pass

        # 录制
        if a.是否录制 and not self.recorder.is_recording(a.key):
            self._start_record(a, room)

    def _on_live_start(self, a, room):
        self._log("检测到开播: %s" % a.主播名称)
        append_record_event(a, "开播", a.直播标题 or "")
        append_snapshot(snapshot_from_room(room))
        text = "「%s」开播了（在线 %s）%s" % (
            a.主播名称, a.显示在线人数 or "-",
            ("｜" + a.直播标题) if a.直播标题 else "")
        if a.是否开播提醒:
            self.notifier.play("开播")
        self.pusher.push("开播", text)
        self._emit("ok", "「%s」开播了（在线 %s）"
                   % (a.主播名称, a.显示在线人数 or "-"))

    def _on_live_end(self, a):
        self._log("检测到关播: %s" % a.主播名称)
        append_record_event(a, "关播", "")
        if self.recorder.is_recording(a.key):
            self.recorder.stop(a.key)
            a.是否正在录制 = False
            a.录制状态 = "已停止"
            if a.是否停止录制提醒:
                self.notifier.play("停止录制")
            self.pusher.push("停止录制", "「%s」已关播，录制自动停止" % a.主播名称)
        if a.是否关播提醒:
            self.notifier.play("关播")
        self.pusher.push("关播", "「%s」已关播" % a.主播名称)
        self._emit("warn", "「%s」已关播" % a.主播名称)

    def _start_record(self, a, room):
        # 清晰度：原软件「直播录制质量」是「档位->启用」字典 + 优先顺序，
        # 按优先级取第一个启用档位选流。
        quality = primary_ratio(a)
        stream = room.stream_for(quality) if hasattr(room, "stream_for") \
            else room.stream_url
        if not stream and room.web_rid:
            detail = self.client.room_by_web_rid(room.web_rid)
            if detail:
                stream = detail.stream_for(quality) if hasattr(
                    detail, "stream_for") else detail.stream_url
        if not stream:
            self._emit("error", "「%s」未取到直播流地址，无法录制" % a.主播名称)
            return
        try:
            out, err = self.recorder.start(
                a, stream,
                quality=quality,
                # RecContent 枚举：0=Video（视频，有视频有声）、1=Audio（音频）
                content=("音频" if int(a.录制内容 or 0) == 1 else "视频"),
                segment_minutes=(a.分段录制几分钟 if a.是否分段录制 else 0),
                stop_minutes=a.录制多少分钟停止 or 0,
                # 录制方式：录制流（ffmpeg）/ 下载流（FLV 下载）。
                # 下载流要带 UA + Referer，否则 CDN 直接拒。
                mode=self._get_config().get("录制方式") or "录制流",
                headers={
                    "User-Agent": getattr(self.client, "ua", "") or "",
                    "Referer": "https://live.douyin.com/",
                })
        except Exception as e:  # noqa: BLE001
            self._emit("error", "「%s」录制启动失败: %s" % (a.主播名称, e))
            return
        if out:
            a.是否正在录制 = True
            a.录制状态 = "录制中"
            append_record_event(a, "开始录制", "%s | %s" % (out, quality))
            if a.是否开始录制提醒:
                self.notifier.play("开始录制")
            self.pusher.push("开始录制",
                             "「%s」开始录制（%s）" % (a.主播名称, quality))
            self._emit("ok", "「%s」开始录制（%s）" % (a.主播名称, quality))

    # ------------------------------------------------------------ 工具

    def _find(self, key):
        for a in self._get_anchors():
            if a.key == key:
                return a
        return None

    def _log(self, text):
        log(text)
        self._emit("debug", text)
