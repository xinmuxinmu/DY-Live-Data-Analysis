# -*- coding: utf-8 -*-
"""本地持久化：主播信息 / 记录数据 / 运行日志

主播信息文件与原软件同构（软件数据/主播信息/_LiveRoomInfo.txt），
这样两边数据可以互相识别。
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time

from . import config
from .models import ANCHOR_FIELDS, Anchor

_lock = threading.RLock()

_MSG = ("【文档内是数据参数，不懂不要修改，改错了加载不进来，只供观看，特意改了中文】")


# ------------------------------------------------------------------ 主播信息

def _iter_dicts(raw):
    """把各种可能的历史格式统一成 dict 序列。

    支持：
      1. {"Msg":..., "主播列表":[...]}      —— 本版本写出的格式
      2. [ {...}, {...} ]                    —— 纯数组
      3. {...}                               —— 原软件的单个模板对象
      4. 每行一个 JSON 对象（JSONL）
    """
    if isinstance(raw, dict):
        lst = raw.get("主播列表")
        if isinstance(lst, list):
            return [d for d in lst if isinstance(d, dict)]
        # 原软件单对象：若只有模板字段（无主播唯一码）则视为空
        return [raw]
    if isinstance(raw, list):
        return [d for d in raw if isinstance(d, dict)]
    return []


def load_anchors():
    out = []
    seen = set()

    # 主文件
    if os.path.isfile(config.ANCHOR_FILE):
        try:
            with open(config.ANCHOR_FILE, encoding="utf-8-sig") as f:
                text = f.read().strip()
        except Exception:  # noqa: BLE001
            text = ""
        raw = None
        if text:
            try:
                raw = json.loads(text)
            except Exception:  # noqa: BLE001
                # JSONL 兜底
                raw = []
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            raw.append(json.loads(line))
                        except Exception:  # noqa: BLE001
                            pass
        for d in _iter_dicts(raw):
            a = Anchor.from_dict(d)
            k = a.key
            if k and k in seen:
                continue
            seen.add(k)
            a.序号 = len(out) + 1
            out.append(a)

    # 兼容原软件「一个主播一个文件」的形态：<主播名>_LiveRoomInfo.txt
    try:
        for name in sorted(os.listdir(config.ANCHOR_DIR)):
            if name == os.path.basename(config.ANCHOR_FILE):
                continue
            if not name.endswith("_LiveRoomInfo.txt"):
                continue
            p = os.path.join(config.ANCHOR_DIR, name)
            try:
                with open(p, encoding="utf-8-sig") as f:
                    d = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            for item in _iter_dicts(d):
                a = Anchor.from_dict(item)
                k = a.key
                if not k or k in seen:
                    continue
                seen.add(k)
                a.序号 = len(out) + 1
                out.append(a)
    except OSError:
        pass

    return out


def save_anchors(anchors):
    """写入主播列表。

    本版本采用「单文件 + 数组」形态（结构清晰、不易写坏）；
    同时保留 Msg 与首个主播的模板字段，便于原软件识别同一份数据。
    """
    with _lock:
        for i, a in enumerate(anchors, start=1):
            a.序号 = i
        payload = {"Msg": _MSG,
                   "主播列表": [a.to_dict() for a in anchors]}
        if anchors:
            payload.update(anchors[0].to_dict())
        tmp = config.ANCHOR_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, config.ANCHOR_FILE)
    return config.ANCHOR_FILE


# ------------------------------------------------------ 记录数据（采集快照）

def append_snapshot(snapshot):
    """按天追加一条采集记录（CSV，UTF-8 BOM，Excel 可直接打开）。"""
    from .models import SNAPSHOT_COLUMNS
    day = time.strftime("%Y-%m-%d")
    path = os.path.join(config.RECORD_DATA_DIR, "%s_直播数据.csv" % day)
    new = not os.path.isfile(path)
    with _lock:
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(SNAPSHOT_COLUMNS)
            w.writerow([_fmt(_snap_val(snapshot, k))
                        for k in SNAPSHOT_COLUMNS])
    return path


def _snap_val(snapshot, key):
    """序号不是 Snapshot 的字段，写行时留空（导出 Excel 时才编号）。"""
    if key == "序号":
        return ""
    return getattr(snapshot, key, "")


def _fmt(v):
    return "" if v is None else v


def load_snapshots(day=None):
    """读取某天的采集记录。"""
    from .models import SNAPSHOT_COLUMNS, Snapshot
    day = day or time.strftime("%Y-%m-%d")
    path = os.path.join(config.RECORD_DATA_DIR, "%s_直播数据.csv" % day)
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            out.append(Snapshot(**{k: row.get(k) for k in SNAPSHOT_COLUMNS}))
    return out


def list_record_days():
    days = []
    for n in sorted(os.listdir(config.RECORD_DATA_DIR), reverse=True):
        if n.endswith("_直播数据.csv"):
            days.append(n[:10])
    return days


# ------------------------------------------------------ 录制事件 / 运行日志

def append_record_event(anchor, action, detail=""):
    day = time.strftime("%Y-%m-%d")
    path = os.path.join(config.RECORD_DATA_DIR, "%s_录制事件.csv" % day)
    new = not os.path.isfile(path)
    with _lock:
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["时间", "主播名称", "动作", "详情"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"),
                        anchor.主播名称, action, detail])
    return path


_LOG_LOCK = threading.RLock()


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    path = os.path.join(config.LOG_DIR, "%s.log" % time.strftime("%Y-%m-%d"))
    with _LOG_LOCK:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass
    return line


def tail_log(n=200):
    path = os.path.join(config.LOG_DIR, "%s.log" % time.strftime("%Y-%m-%d"))
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-n:])
