# -*- coding: utf-8 -*-
"""抖音直播接口客户端（纯标准库）

接口地址来自原软件 DY直播数据分析.core.exe 内嵌字符串，未做猜测。
签名 a_bogus 由 dy.abogus 纯 Python 计算（已与原始 JS 逐字节比对一致）。
"""
from __future__ import annotations

import gzip
import http.cookiejar
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .abogus import DEFAULT_UA, a_bogus

FEED_URL = "https://webcast.amemv.com/webcast/feed/"
PROFILE_URL = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
ENTER_URL = "https://live.douyin.com/webcast/room/web/enter/"
HOME_URL = "https://live.douyin.com/"

# 抖音号 -> 用户信息。实测可用（无需 a_bogus、无需登录）：
#   GET https://www.iesdouyin.com/web/api/v2/user/info/?unique_id=XINMU__
#   -> {"user_info": {"nickname": "歆慕", "sec_uid": "MS4wLjABAAAA...",
#                     "unique_id": "XINMU__", "mplatform_followers_count": 800,
#                     "total_favorited": "21233", ...}}
# 原软件的「抖音号」字段即 unique_id（见 _LiveRoomInfo.txt schema）。
IES_USER_URL = "https://www.iesdouyin.com/web/api/v2/user/info/"

COMMON_WEB = {
    "aid": "6383", "app_name": "douyin_web", "live_id": "1",
    "device_platform": "web", "language": "zh-CN", "enter_from": "web_live",
    "cookie_enabled": "true", "screen_width": "1920", "screen_height": "1080",
    "browser_language": "zh-CN", "browser_platform": "Win32",
    "browser_name": "Chrome", "browser_version": "131.0.0.0",
}

COMMON_WEBAPP = {
    "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
    "publish_video_strategy_type": "2", "version_code": "170400",
    "version_name": "17.4.0", "cookie_enabled": "true", "platform": "PC",
    "downlink": "10",
}

STATUS_TEXT = {2: "直播中", 4: "已结束"}

# 清晰度档位（抖音 flv_pull_url 的键），从高到低
RESOLUTION_ORDER = ["FULL_HD1", "HD1", "SD1", "SD2"]
RESOLUTION_LABEL = {
    "FULL_HD1": "原画", "HD1": "超清", "SD1": "标清", "SD2": "流畅",
}
# 界面清晰度 -> 期望档位（依次回退）
# 清晰度档位映射。对应原软件 LiveRatio 枚举
# （LanGuang 蓝光 / YuanHua 原画 / ChaoQing 超清 / GaoQing 高清 / BiaoQing 标清），
# 抖音 flv_pull_url 只提供 FULL_HD1 / HD1 / SD1 / SD2 四档，故 5 档映射到 4 档。
# 每个 key 后面的列表是「取不到时按序回退」的顺序。
QUALITY_TO_RESOLUTION = {
    "蓝光": ["FULL_HD1", "HD1", "SD1", "SD2"],
    "原画": ["FULL_HD1", "HD1", "SD1", "SD2"],
    "超清": ["HD1", "FULL_HD1", "SD1", "SD2"],
    "高清": ["SD1", "HD1", "FULL_HD1", "SD2"],
    "标清": ["SD2", "SD1", "HD1", "FULL_HD1"],
    # 兼容早期写法的「流畅」
    "流畅": ["SD2", "SD1", "HD1", "FULL_HD1"],
}


def human_count(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 10000:
        return "%.1f万" % (n / 10000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


class DouyinError(RuntimeError):
    pass


class LiveRoom:
    __slots__ = ("nickname", "display_id", "sec_uid", "uid", "room_id",
                 "web_rid", "status", "user_count", "user_count_str",
                 "total_user", "follower_count", "like_count", "title",
                 "city", "has_goods", "url", "collected_at", "stream_url",
                 "streams")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        if self.streams is None:
            self.streams = {}
        if not self.stream_url and self.streams:
            self.stream_url = next(iter(self.streams.values()))

    @property
    def status_text(self):
        return STATUS_TEXT.get(self.status, str(self.status or ""))

    @property
    def live_url(self):
        if self.url:
            return self.url
        if self.web_rid:
            return "https://live.douyin.com/%s" % self.web_rid
        return ""

    def stream_for(self, quality="原画"):
        """按清晰度取流地址，取不到就逐档回退，最后返回默认流。"""
        if not self.streams:
            return self.stream_url
        for res in QUALITY_TO_RESOLUTION.get(quality or "原画",
                                             RESOLUTION_ORDER):
            url = self.streams.get(res)
            if url:
                return url
        return self.stream_url or next(iter(self.streams.values()))

    @property
    def quality_list(self):
        """本房间实际可用的清晰度（界面展示用）。"""
        return [RESOLUTION_LABEL.get(r, r) for r in RESOLUTION_ORDER
                if self.streams.get(r)]


class DouyinLive:
    def __init__(self, user_agent=DEFAULT_UA, timeout=20, delay=1.0,
                 log=None):
        self.ua = user_agent or DEFAULT_UA
        self.timeout = timeout
        self.delay = delay
        self._log = log or (lambda *a: None)
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self._booted = False

    # ------------------------------------------------------------ 底层

    def _raw(self, url, headers=None, tries=3):
        h = {
            "User-Agent": self.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
        }
        if headers:
            h.update(headers)
        last = None
        for i in range(tries):
            try:
                req = urllib.request.Request(url, headers=h)
                with self._opener.open(req, timeout=self.timeout) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    return data
            except urllib.error.HTTPError as e:
                last = "HTTP %s" % e.code
            except Exception as e:  # noqa: BLE001
                last = str(e)
            time.sleep(0.8 + i)
        raise DouyinError("请求失败(%s): %s" % (last, url[:80]))

    def bootstrap(self, force=False):
        if self._booted and not force:
            return
        try:
            self._raw(HOME_URL, headers={"Referer": HOME_URL})
        except DouyinError:
            pass
        self._log("cookie: %s" % [c.name for c in self._jar])
        self._booted = True

    def _api(self, base, params, referer=None):
        self.bootstrap()
        p = {k: str(v) for k, v in params.items()}
        qs = urllib.parse.urlencode(p)
        url = "%s?%s&a_bogus=%s" % (base, qs, a_bogus(qs, self.ua))
        h = {"Referer": referer} if referer else {}
        raw = self._raw(url, headers=h)
        if not raw.strip():
            raise DouyinError("接口返回空响应（被风控拦截）: %s" % base)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise DouyinError("响应非 JSON: %s" % e)

    # ------------------------------------------------------------ 接口

    def hot_rooms(self, count=15, offset=0):
        j = self._api(FEED_URL,
                      dict(COMMON_WEB, count=count, offset=offset, type_id=0),
                      referer=HOME_URL)
        if j.get("status_code") not in (0, None):
            raise DouyinError("feed 接口错误: %s" % j.get("status_msg"))
        out = []
        for item in (j.get("data") or []):
            d = item.get("data") if isinstance(item, dict) else None
            if isinstance(d, dict):
                r = self._from_room_obj(d)
                if r.nickname:
                    out.append(r)
        return out

    def user_profile(self, sec_user_id):
        j = self._api(PROFILE_URL, dict(COMMON_WEBAPP,
                                        sec_user_id=sec_user_id),
                      referer="https://www.douyin.com/user/%s" % sec_user_id)
        u = j.get("user")
        if not isinstance(u, dict):
            raise DouyinError("未取到用户信息: %s" % j.get("status_msg"))
        return u

    def user_by_unique_id(self, unique_id):
        """按「抖音号」(unique_id) 查用户，返回 user_info 字典。

        对应原软件的「抖音号」识别能力。用的是 iesdouyin 公开接口，
        不需要 a_bogus、不需要登录。
        注意：纯数字输入会被抖音当作 short_id 解析，可能与预期不同。
        """
        uid = (unique_id or "").strip().lstrip("@")
        if not uid:
            raise DouyinError("抖音号为空")
        url = IES_USER_URL + "?" + urllib.parse.urlencode({"unique_id": uid})
        raw = self._raw(url, headers={"Referer": "https://www.iesdouyin.com/"})
        if not raw.strip():
            raise DouyinError("抖音号接口返回空响应（可能被风控）")
        try:
            j = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise DouyinError("抖音号接口响应非 JSON: %s" % e)
        ui = j.get("user_info")
        if not isinstance(ui, dict) or not ui.get("sec_uid"):
            raise DouyinError("未找到抖音号「%s」对应的用户" % uid)
        return ui

    # 输入类型
    KIND_HOME = "主页链接"
    KIND_LIVE = "直播间链接"
    KIND_SEC = "唯一码"
    KIND_UID = "抖音号"

    def resolve_input(self, text):
        """把用户输入统一解析成主播。

        支持：主播主页链接 / 直播间链接 / sec_uid / 抖音号（含 @抖音号）。
        返回 dict(kind, sec_uid, user_info, web_rid)，失败抛 DouyinError。
        """
        s = (text or "").strip()
        if not s:
            raise DouyinError("请输入主播链接或抖音号")

        # 1) 主页链接 / 直接给 sec_uid
        sec = self.parse_sec_uid(s)
        if sec:
            u = self.user_profile(sec)
            return dict(kind=self.KIND_SEC, sec_uid=sec, user_info=u, web_rid="")

        # 2) 直播间链接（live.douyin.com/数字）
        rid = self.parse_web_rid(s)
        if rid:
            room = self.room_by_web_rid(rid)
            if room and room.sec_uid:
                try:
                    u = self.user_profile(room.sec_uid)
                except DouyinError:
                    u = {}
                return dict(kind=self.KIND_LIVE, sec_uid=room.sec_uid,
                            user_info=u, web_rid=rid)
            raise DouyinError(
                "直播间链接（live.douyin.com/%s）受抖音风控限制取不到主播信息，"
                "请改用主播主页链接或抖音号" % rid)

        # 3) 抖音号
        if re.fullmatch(r"@?[A-Za-z0-9_\-\.]{2,30}", s):
            ui = self.user_by_unique_id(s)
            return dict(kind=self.KIND_UID, sec_uid=ui.get("sec_uid"),
                        user_info=ui, web_rid="")

        raise DouyinError("无法识别的输入：%s（请用主播主页链接或抖音号）" % s[:40])

    def room_by_web_rid(self, web_rid):
        """按 web_rid 取房间详情（该接口当前常被风控，失败返回 None）。"""
        try:
            j = self._api(ENTER_URL, dict(COMMON_WEB, web_rid=web_rid),
                          referer="https://live.douyin.com/%s" % web_rid)
        except DouyinError:
            return None
        rooms = ((j.get("data") or {}).get("data")) or []
        if isinstance(rooms, list) and rooms:
            return self._from_room_obj(rooms[0])
        return None

    def live_of_user(self, sec_user_id):
        """查询主播当前直播状态；未开播返回 (None, user)。"""
        u = self.user_profile(sec_user_id)
        if not u.get("live_status"):
            return None, u
        rd = u.get("room_data")
        if isinstance(rd, str) and rd.strip():
            try:
                rd = json.loads(rd)
            except Exception:  # noqa: BLE001
                rd = None
        r = self._from_room_obj(rd or {}, user=u)
        r.room_id = r.room_id or u.get("room_id_str") or u.get("room_id")
        r.sec_uid = r.sec_uid or u.get("sec_uid")
        r.nickname = r.nickname or u.get("nickname")
        r.uid = r.uid or u.get("uid") or u.get("id_str")
        r.display_id = r.display_id or u.get("display_id") or u.get("short_id")
        r.city = r.city or u.get("city") or u.get("location_city")
        if r.follower_count is None:
            r.follower_count = u.get("follower_count")
        if r.user_count is None:
            r.user_count = (rd or {}).get("user_count")
        if r.status is None:
            r.status = (rd or {}).get("status")
        if r.user_count is not None and not r.user_count_str:
            r.user_count_str = human_count(r.user_count)
        if r.web_rid:
            r.url = "https://live.douyin.com/%s" % r.web_rid
        return r, u

    # ------------------------------------------------------------ 归一化

    @staticmethod
    def _from_room_obj(d, user=None):
        owner = d.get("owner") or {}
        if not owner and user:
            owner = user
        follow = owner.get("follow_info") or {}
        stats = d.get("stats") or {}
        view = d.get("room_view_stats") or {}
        web_rid = owner.get("web_rid") or d.get("web_rid") or None
        if web_rid:
            url = "https://live.douyin.com/%s" % web_rid
        else:
            url = d.get("share_url") or None
        stream = None
        streams = {}
        su = d.get("stream_url") or {}
        if isinstance(su, dict):
            flv = su.get("flv_pull_url") or {}
            if isinstance(flv, dict):
                for res in RESOLUTION_ORDER:
                    if flv.get(res):
                        streams[res] = flv[res]
                # 兼容未按标准命名的键
                for k, v in flv.items():
                    if v and k not in streams:
                        streams[k] = v
            stream = streams.get("FULL_HD1") or (
                next(iter(streams.values())) if streams else None)
            stream = stream or su.get("hls_pull_url")
        return LiveRoom(
            nickname=owner.get("nickname") or d.get("nickname"),
            display_id=owner.get("display_id") or owner.get("short_id"),
            sec_uid=owner.get("sec_uid"),
            uid=owner.get("id_str") or owner.get("id") or d.get("owner_user_id"),
            room_id=d.get("id_str") or d.get("id"),
            web_rid=web_rid,
            status=d.get("status"),
            user_count=d.get("user_count"),
            user_count_str=(view.get("display_short")
                            or stats.get("user_count_str")),
            total_user=(stats.get("total_user") or d.get("web_count")),
            follower_count=follow.get("follower_count"),
            like_count=d.get("like_count"),
            title=d.get("title"),
            city=(owner.get("city") or owner.get("location_city")
                  or d.get("location")),
            has_goods=bool(d.get("has_commerce_goods")),
            url=url,
            collected_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            stream_url=stream,
            streams=streams,
        )

    # ------------------------------------------------------------ 解析工具

    @staticmethod
    def parse_sec_uid(text):
        if not text:
            return ""
        text = text.strip()
        m = re.search(r"/user/([A-Za-z0-9_\-]+)", text)
        if m:
            return m.group(1)
        m = re.search(r"sec_user_id=([A-Za-z0-9_\-]+)", text)
        if m:
            return m.group(1)
        if re.fullmatch(r"MS4wLjAB[A-Za-z0-9_\-]+", text):
            return text
        return ""

    @staticmethod
    def parse_web_rid(text):
        if not text:
            return ""
        text = text.strip()
        m = re.search(r"live\.douyin\.com/(\d+)", text)
        if m:
            return m.group(1)
        if re.fullmatch(r"\d{6,}", text):
            return text
        return ""

    def sleep(self):
        if self.delay:
            time.sleep(self.delay * (0.5 + random.random()))
