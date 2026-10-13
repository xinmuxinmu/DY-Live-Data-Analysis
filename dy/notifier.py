# -*- coding: utf-8 -*-
"""提醒 / 提示音

优先播放 软件数据/提示音/ 下的 wav（与原软件同名文件兼容），
找不到就退回系统提示音。纯标准库（winsound）。
"""
from __future__ import annotations

import os
import threading

from . import config
from .store import log

try:
    import winsound
except ImportError:  # 非 Windows
    winsound = None

SOUND_DIR = os.path.join(config.DATA_DIR, "提示音")

# 事件 -> 提示音文件名（与原软件提示音包命名一致）
SOUND_FILES = {
    "开播": "开播提示音.wav",
    "关播": "关播提示音.wav",
    "开始录制": "开始录制提示音.wav",
    "停止录制": "停止录制提示音.wav",
    "在线人数": "在线人数提示音.wav",
}

# 事件 -> 系统提示音（无自定义文件时）
SYSTEM_SOUND = {
    "开播": "SystemAsterisk",
    "关播": "SystemHand",
    "开始录制": "SystemExclamation",
    "停止录制": "SystemQuestion",
    "在线人数": "SystemDefault",
}


class Notifier:
    def __init__(self, get_config=None):
        self._cfg = get_config or (lambda: {})
        os.makedirs(SOUND_DIR, exist_ok=True)

    def _enabled(self, event):
        cfg = self._cfg() or {}
        mapping = {
            "开播": "开播提醒",
            "关播": "关播提醒",
            "开始录制": "开始录制提醒",
            "停止录制": "停止录制提醒",
            "在线人数": "在线人数提醒",
        }
        key = mapping.get(event)
        if key is None:
            return True
        return bool(cfg.get(key, True))

    def play(self, event):
        """异步播放提示音，不阻塞监控线程。"""
        if not self._enabled(event):
            return False
        threading.Thread(target=self._play_sync, args=(event,),
                         daemon=True).start()
        return True

    def _play_sync(self, event):
        if winsound is None:
            return
        cfg = self._cfg() or {}
        if cfg.get("提示音") == "静音":
            return
        path = os.path.join(SOUND_DIR, SOUND_FILES.get(event, ""))
        try:
            if os.path.isfile(path):
                winsound.PlaySound(path, winsound.SND_FILENAME |
                                   winsound.SND_NODEFAULT)
            else:
                alias = SYSTEM_SOUND.get(event, "SystemDefault")
                winsound.PlaySound(alias, winsound.SND_ALIAS |
                                   winsound.SND_NODEFAULT)
        except Exception as e:  # noqa: BLE001
            log("提示音播放失败(%s): %s" % (event, e))

    def has_custom_sounds(self):
        return any(os.path.isfile(os.path.join(SOUND_DIR, f))
                   for f in SOUND_FILES.values())
