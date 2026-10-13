# -*- coding: utf-8 -*-
"""路径与全局配置"""
from __future__ import annotations

import json
import os
import shutil
import sys

APP_NAME = "DY直播数据分析"
APP_VERSION = "2.0.0"
APP_TITLE = "%s（Python 版）  v%s" % (APP_NAME, APP_VERSION)


def _base_dir():
    """程序根目录（兼容 PyInstaller 打包后的 exe）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _base_dir()
DATA_DIR = os.path.join(BASE_DIR, "软件数据")
ANCHOR_DIR = os.path.join(DATA_DIR, "主播信息")
ANCHOR_FILE = os.path.join(ANCHOR_DIR, "_LiveRoomInfo.txt")
RECORD_DATA_DIR = os.path.join(DATA_DIR, "记录数据")
VIDEO_DIR = os.path.join(DATA_DIR, "录制文件")
AUDIO_CACHE_DIR = os.path.join(DATA_DIR, "缓存音频")
LOG_DIR = os.path.join(DATA_DIR, "运行日志")
CONFIG_FILE = os.path.join(DATA_DIR, "配置.json")

for _d in (DATA_DIR, ANCHOR_DIR, RECORD_DATA_DIR, VIDEO_DIR,
           AUDIO_CACHE_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)


# ------------------------------------------------------------------ ffmpeg

def find_ffmpeg():
    """按优先级查找 ffmpeg：程序自带 -> 兼容原目录布局 -> 环境变量 -> PATH。"""
    cands = [
        os.path.join(BASE_DIR, "ffmpeg", "ffmpeg.exe"),
        os.path.join(BASE_DIR, "JHlib", "ffmpeg", "ffmpeg.exe"),
    ]
    env = os.environ.get("DY_FFMPEG")
    if env:
        cands.insert(0, env)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    w = shutil.which("ffmpeg")
    return w or ""


FFMPEG = find_ffmpeg()


def find_ffplay():
    """查找 ffplay（直播流预览用）。"""
    if FFMPEG:
        p = os.path.join(os.path.dirname(FFMPEG), "ffplay.exe")
        if os.path.isfile(p):
            return p
    w = shutil.which("ffplay")
    return w or ""


def find_ffprobe():
    if FFMPEG:
        p = os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe")
        if os.path.isfile(p):
            return p
    w = shutil.which("ffprobe")
    return w or ""


FFPLAY = find_ffplay()
FFPROBE = find_ffprobe()


# ------------------------------------------------------------------ 默认配置

DEFAULT_CONFIG = {
    "监控间隔秒": 30,
    # 清晰度：与原软件一致，是「档位 -> 是否启用」字典（LiveRatio 枚举），
    # key 顺序即优先顺序（蓝光最高）
    "默认录制质量": {"LanGuang": True, "YuanHua": True, "ChaoQing": True,
                 "GaoQing": True, "BiaoQing": True},
    "默认录制内容": 0,              # RecContent 枚举：0=视频 1=音频
    # 录制方式：对应原软件 Frm_Main 的 rdbtn_录制流 / rdbtn_下载流。
    #   录制流 = ffmpeg 录制，输出 ts/mp4，播放器可直接播放
    #   下载流 = 直接下载 FLV 流，输出 .flv，无法播放需转码
    # 原软件的说明：「如果选择录制，无法录制，就选下载方式。反之亦然。」
    "录制方式": "录制流",
    "录制多少分钟停止": 0,
    "是否分段录制": False,
    "分段录制几分钟": 30,
    "录制目录": VIDEO_DIR,
    "录制完成后无损转码MP4": False,
    "转码完成后删除源文件": False,
    "默认转码预设": "MP4（H.264，通用）",
    "开播提醒": True,
    "关播提醒": True,
    "开始录制提醒": True,
    "停止录制提醒": True,
    "在线人数提醒": False,
    "在线人数提醒阈值": 10000,
    "提示音": "默认",
    "静默时段": "",
    "启动时自动开启监控": False,
    "窗口最小化到托盘": False,
    "添加时默认监控": True,
    "添加时默认录制": False,
    "使用内置浏览器": False,
    "浏览器用户数据目录": os.path.join(DATA_DIR, "浏览器用户数据"),
    "推送": {
        "启用": False,
        "平台": "企业微信机器人",
        "Webhook": "",
        "标题前缀": "",
        "事件": {
            "开播": True, "关播": True, "开始录制": True,
            "停止录制": True, "在线人数": False,
        },
    },
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:  # noqa: BLE001
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    return CONFIG_FILE
