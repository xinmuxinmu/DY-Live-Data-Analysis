# -*- coding: utf-8 -*-
"""消息推送（对应原软件 UserC_PushMsgSet）

支持企业微信机器人、钉钉机器人、飞书机器人，以及任意自定义 Webhook。
纯标准库实现（urllib），不需要任何第三方包。

事件类型：开播 / 关播 / 开始录制 / 停止录制 / 在线人数
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from .store import log

# 推送平台 -> 默认消息体构造
PLATFORMS = ["企业微信机器人", "钉钉机器人", "飞书机器人", "自定义 Webhook"]

DEFAULT_EVENTS = {
    "开播": True, "关播": True, "开始录制": True,
    "停止录制": True, "在线人数": False,
}


def _build_body(platform, text):
    """按平台组装 JSON 消息体。"""
    if platform == "企业微信机器人":
        return {"msgtype": "text", "text": {"content": text}}
    if platform == "钉钉机器人":
        return {"msgtype": "text", "text": {"content": text}}
    if platform == "飞书机器人":
        return {"msg_type": "text", "content": {"text": text}}
    # 自定义：同时给出常见字段，方便对接自建服务
    return {"text": text, "content": text, "message": text,
            "timestamp": int(time.time())}


class Pusher:
    """读取配置里的推送设置，按事件异步推送。"""

    def __init__(self, get_config=None):
        self._cfg = get_config or (lambda: {})

    # ------------------------------------------------------------ 配置

    def _conf(self):
        cfg = self._cfg() or {}
        p = cfg.get("推送") or {}
        return p if isinstance(p, dict) else {}

    def enabled(self):
        p = self._conf()
        return bool(p.get("启用") and p.get("Webhook"))

    def want(self, event):
        p = self._conf()
        ev = p.get("事件") or {}
        if event in ev:
            return bool(ev[event])
        return DEFAULT_EVENTS.get(event, False)

    # ------------------------------------------------------------ 发送

    def push(self, event, text, force=False):
        """异步推送。force=True 时忽略事件开关（用于测试）。"""
        if not self.enabled():
            return False
        if not force and not self.want(event):
            return False
        threading.Thread(target=self._send, args=(event, text),
                         daemon=True).start()
        return True

    def _send(self, event, text):
        p = self._conf()
        url = (p.get("Webhook") or "").strip()
        if not url:
            return
        platform = p.get("平台") or "企业微信机器人"
        prefix = p.get("标题前缀") or ""
        body_text = "%s【%s】%s" % (prefix, event, text) if prefix \
            else "【%s】%s" % (event, text)
        payload = _build_body(platform, body_text)
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            log("推送成功[%s]: %s" % (event, body_text))
        except Exception as e:  # noqa: BLE001
            log("推送失败[%s]: %s" % (event, e))

    def test(self):
        """同步测试推送，返回 (成功, 提示信息)。"""
        p = self._conf()
        url = (p.get("Webhook") or "").strip()
        if not url:
            return False, "未填写 Webhook 地址"
        platform = p.get("平台") or "企业微信机器人"
        payload = _build_body(platform, "【测试】DY直播数据分析 推送连通性测试")
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read().decode("utf-8", "replace")
            return True, "已发送。返回：%s" % body[:200]
        except Exception as e:  # noqa: BLE001
            return False, "发送失败：%s" % e


def validate_webhook(url):
    """粗略校验 Webhook 地址。"""
    if not url:
        return "未填写"
    try:
        u = urllib.parse.urlparse(url)
    except Exception:  # noqa: BLE001
        return "地址格式错误"
    if u.scheme not in ("http", "https"):
        return "必须以 http:// 或 https:// 开头"
    if not u.netloc:
        return "缺少域名"
    return ""
