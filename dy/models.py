# -*- coding: utf-8 -*-
"""数据模型：主播配置 / 采集快照 / 录制任务"""
from __future__ import annotations

import time

# 与原软件「主播信息/<用户id>_LiveRoomInfo.txt」完全一致的字段顺序。
# 依据（非推测）：
#   1) core.exe 内嵌字符串堆（偏移 5024184~5024520）中 WriteLiveRecInfo 的
#      写出字段清单；
#   2) 原软件自带真实数据文件（公开版/软件数据/…/主播信息/*_LiveRoomInfo.txt，
#      5 份键序完全一致）逐项比对。
# 注意：序号 / 粉丝量 / 点赞量 在原软件里**是要落盘的**（位于 是否选择 之后、
# 主播名称 前后），早前版本误把它们当作运行期字段，已修正。
ANCHOR_FIELDS = [
    "是否选择", "序号", "主播名称", "粉丝量", "点赞量",
    "是否监控", "是否录制", "录制内容",
    "录制多少分钟停止", "是否分段录制", "分段录制几分钟", "直播录制质量",
    "主播唯一码", "用户id", "房间号", "抖音号", "备注", "添加时间",
    "是否登录状态访问直播间", "是否转写话术文案",
    "是否开播提醒", "是否关播提醒", "是否开始录制提醒",
    "是否停止录制提醒", "是否在线人数提醒",
    "是否讲解商品记录保存", "是否购物车商品记录保存",
]

# 直播录制质量：原软件里是一个「档位 -> 是否启用」的字典（LiveRatio 枚举），
# 不是单个清晰度字符串。key 顺序即优先级顺序（蓝光最高）。
# 依据：LiveRatio.cs 枚举 + 原软件真实数据文件中的
#       "直播录制质量": {"LanGuang":true,"YuanHua":true,"ChaoQing":true,
#                        "GaoQing":true,"BiaoQing":true}
LIVE_RATIO_KEYS = ["LanGuang", "YuanHua", "ChaoQing", "GaoQing", "BiaoQing"]
LIVE_RATIO_NAMES = {
    "LanGuang": "蓝光", "YuanHua": "原画", "ChaoQing": "超清",
    "GaoQing": "高清", "BiaoQing": "标清",
}
NAME_TO_RATIO_KEY = {v: k for k, v in LIVE_RATIO_NAMES.items()}
QUALITY_CHOICES = [LIVE_RATIO_NAMES[k] for k in LIVE_RATIO_KEYS]


def default_ratio_dict():
    return {k: True for k in LIVE_RATIO_KEYS}


def normalize_ratio(v):
    """把 直播录制质量 归一成 {档位key: bool} 字典。"""
    d = default_ratio_dict()
    if isinstance(v, dict):
        for k in LIVE_RATIO_KEYS:
            if k in v:
                d[k] = as_bool(v[k])
    elif isinstance(v, str) and v:
        # 兼容旧的单字符串写法（本版本早期格式）
        key = NAME_TO_RATIO_KEY.get(v)
        if key:
            d = {k: (k == key) for k in LIVE_RATIO_KEYS}
    return d


def enabled_ratios(anchor):
    """按优先级返回该主播启用的清晰度中文名列表。"""
    d = normalize_ratio(getattr(anchor, "直播录制质量", None))
    return [LIVE_RATIO_NAMES[k] for k in LIVE_RATIO_KEYS if d.get(k)]


def primary_ratio(anchor):
    """按优先级返回第一个启用的清晰度中文名（录制时用它选流）。"""
    lst = enabled_ratios(anchor)
    return lst[0] if lst else "原画"


ANCHOR_DEFAULTS = {
    "是否选择": False, "序号": 0, "主播名称": "", "粉丝量": "0", "点赞量": 0,
    "是否监控": False, "是否录制": False,
    "录制内容": 0, "录制多少分钟停止": 0, "是否分段录制": False,
    "分段录制几分钟": 0, "直播录制质量": None, "主播唯一码": "",
    "用户id": "", "房间号": "", "抖音号": "", "备注": "",
    "添加时间": "0001-01-01T00:00:00",
    "是否登录状态访问直播间": False, "是否转写话术文案": False,
    "是否开播提醒": False, "是否关播提醒": False, "是否开始录制提醒": False,
    "是否停止录制提醒": False, "是否在线人数提醒": False,
    "是否讲解商品记录保存": False, "是否购物车商品记录保存": False,
}

# 运行期状态（不落盘）
RUNTIME_DEFAULTS = {
    "是否开播": False,
    "是否正在录制": False,
    "是否正在监控": False,
    "录制状态": "未录制",        # 未录制 / 录制中 / 已停止
    # 最后监控时间：原软件 LiveRecInfo 里唯一的监控时间成员
    # （[JsonIgnore] DateTime，运行期、不落盘），主列表「监控时间」列
    # 显示的就是它，格式 HH:mm:ss。
    "最后监控时间": "",
    "在线人数": None,
    "显示在线人数": "",
    "最高在线人数": 0,
    "直播中新增粉丝数量": 0,
    "直播场观总人数": 0,
    "直播场观进入直播间人数": 0,
    "直播场观未进入直播间人数": 0,
    "已录时间": 0,
    "直播标题": "",
    "直播地址": "",
    "错误": "",
}

# 录制内容（对应原软件 RecContent 枚举：Video / Audio）
RECORD_CONTENT_CHOICES = ["视频", "音频"]

# ---- 字段类型归一 ----------------------------------------------------------
# 原软件的数据文件里，布尔字段可能存成 0/1 或 "0"/"1"/"True"/"False"，
# 数值字段可能存成字符串。若不归一，Python 的 "0" 会被判为 True，
# 导致「明明没开监控却显示已开」这类问题。加载时统一转换。
_BOOL_FIELDS = frozenset(k for k in ANCHOR_FIELDS if k.startswith("是否"))
_INT_FIELDS = frozenset(["序号", "点赞量", "录制内容", "录制多少分钟停止",
                         "分段录制几分钟"])
_FALSE_STR = frozenset(["", "0", "false", "none", "null", "否", "no", "n"])


def as_bool(v):
    """把各种表示「假」的值正确识别为 False。"""
    if isinstance(v, str):
        return v.strip().lower() not in _FALSE_STR
    return bool(v)


def as_int(v, default=0):
    if v is None or v == "":
        return default
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def coerce_field(key, value):
    """按字段语义归一化外部数据。"""
    if key in _BOOL_FIELDS:
        return as_bool(value)
    if key in _INT_FIELDS:
        return as_int(value, 0)
    if key == "直播录制质量":
        return normalize_ratio(value)
    if key == "粉丝量":
        return "" if value is None else str(value)
    return value


class Anchor:
    """一个被监控的主播。"""

    __slots__ = tuple(ANCHOR_FIELDS) + tuple(RUNTIME_DEFAULTS)

    def __init__(self, **kw):
        for k, v in ANCHOR_DEFAULTS.items():
            setattr(self, k, coerce_field(k, kw.get(k, v)))
        for k, v in RUNTIME_DEFAULTS.items():
            setattr(self, k, kw.get(k, v))
        # 兼容运行期字段
        for k in RUNTIME_DEFAULTS:
            if k in kw:
                setattr(self, k, kw[k])

    # ------------------------------------------------------------ 持久化

    def to_dict(self):
        return {k: getattr(self, k) for k in ANCHOR_FIELDS}

    def to_full_dict(self):
        """含运行期状态（用于进程内快照 / 记录导出）。"""
        d = self.to_dict()
        d.update({k: getattr(self, k) for k in RUNTIME_DEFAULTS})
        return d

    @classmethod
    def from_dict(cls, d):
        a = cls()
        for k in ANCHOR_FIELDS:
            if k in d:
                setattr(a, k, coerce_field(k, d[k]))
        # 运行期字段（如果来源里带了，例如进程内快照）
        for k in RUNTIME_DEFAULTS:
            if k in d:
                setattr(a, k, d[k])
        return a

    # ------------------------------------------------------------ 便捷属性

    @property
    def key(self):
        """唯一标识：优先 sec_uid。"""
        return self.主播唯一码 or self.房间号 or self.抖音号 or self.主播名称

    @property
    def live_url(self):
        if self.直播地址:
            return self.直播地址
        if self.房间号:
            return "https://live.douyin.com/%s" % self.房间号
        return ""

    @property
    def video_dir(self):
        from . import config
        name = _safe_name(self.主播名称 or self.key or "未知主播")
        p = config.VIDEO_DIR
        import os
        return os.path.join(p, name)

    def touch(self):
        self.最后监控时间 = time.strftime("%Y-%m-%d %H:%M:%S")

    def __repr__(self):
        return "<Anchor %s 监控=%s 录制=%s>" % (
            self.主播名称, self.是否监控, self.是否录制)


def _safe_name(s):
    bad = '<>:"/\\|?*'
    for c in bad:
        s = s.replace(c, "_")
    return s.strip() or "未知主播"


class Snapshot:
    """一次采集的数据快照（用于记录数据导出）。

    列名与顺序取自 core.exe 内嵌字符串（「开播记录.csv / 关播记录.csv」表头）。
    """

    __slots__ = ("记录时间", "开播时间", "直播标题", "主播名称", "主播唯一码",
                 "粉丝量", "点赞量", "在线人数", "最高在线人数",
                 "新增粉丝数量", "场观总人数", "场观进入直播间人数",
                 "场观未进入直播间人数", "直播状态", "城市", "有商品",
                 "直播地址", "采集时间")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def to_row(self, index=None):
        vals = [getattr(self, k) for k in self.__slots__]
        return ([index] + vals) if index is not None else vals


SNAPSHOT_COLUMNS = ["序号", "记录时间", "开播时间", "直播标题", "主播名称",
                    "主播唯一码", "粉丝量", "点赞量", "在线人数",
                    "最高在线人数", "新增粉丝数量", "场观总人数",
                    "场观进入直播间人数", "场观未进入直播间人数",
                    "直播状态", "城市", "有商品", "直播地址", "采集时间"]

# 原软件「记录数据/开播记录.csv」的表头（不含序号）
OPEN_RECORD_COLUMNS = ["记录时间", "开播时间", "直播标题", "粉丝量", "点赞量",
                       "在线人数", "最高在线人数", "新增粉丝数量", "场观总人数",
                       "场观进入直播间人数", "场观未进入直播间人数"]

CLOSE_RECORD_COLUMNS = ["关播时间", "直播标题", "粉丝量", "点赞量", "在线人数",
                        "最高在线人数", "新增粉丝数量", "场观总人数",
                        "场观进入直播间人数", "场观未进入直播间人数"]


def snapshot_from_room(room, anchor=None):
    """把 douyin.LiveRoom 转成 Snapshot。"""
    now = room.collected_at or time.strftime("%Y-%m-%d %H:%M:%S")
    return Snapshot(
        记录时间=now,
        开播时间=now if (room.status == 2) else "",
        直播标题=room.title or "",
        主播名称=room.nickname or (anchor.主播名称 if anchor else ""),
        主播唯一码=room.sec_uid or (anchor.主播唯一码 if anchor else ""),
        粉丝量=room.follower_count,
        点赞量=room.like_count,
        在线人数=room.user_count,
        最高在线人数=room.user_count,
        新增粉丝数量=(getattr(anchor, "直播中新增粉丝数量", 0) if anchor else 0),
        场观总人数=room.total_user,
        场观进入直播间人数=getattr(anchor, "直播场观进入直播间人数", 0)
        if anchor else 0,
        场观未进入直播间人数=getattr(anchor, "直播场观未进入直播间人数", 0)
        if anchor else 0,
        直播状态=room.status_text,
        城市=room.city or "",
        有商品="是" if room.has_goods else "否",
        直播地址=room.live_url,
        采集时间=now,
    )
