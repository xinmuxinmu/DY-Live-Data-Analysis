# -*- coding: utf-8 -*-
"""对话框（PySide6）

对应原软件：
  Frm_AddLiveRoom   -> AddAnchorDialog       添加主播
  Frm_SetRoomInfo   -> AnchorSettingsDialog  主播独立设置
  （全局设置）       -> SettingsDialog        监控 / 录制 / 提醒 / 提示音

控件命名尽量沿用原软件的字段名（txt_LiveUrl / ckb_AddDefaultMonitor /
ckb_AddDefaultRec / ckb_IsSplitRec / nud_SplitRecNumMinute …），
便于对照维护。
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QTextEdit, QVBoxLayout, QWidget)

from dy.douyin import DouyinError, DouyinLive
from dy.models import (QUALITY_CHOICES, RECORD_CONTENT_CHOICES, Anchor,
                       LIVE_RATIO_KEYS, LIVE_RATIO_NAMES, normalize_ratio)


class QualityChecks(QWidget):
    """直播录制质量：5 个档位的多选。

    对应原软件 `LiveRecInfo.直播录制质量`（Dictionary<LiveRatio,bool>）
    与右键「录制清晰度与优先顺序」。勾选表示启用，排列顺序即优先级
    （蓝光 > 原画 > 超清 > 高清 > 标清），录制时取第一个可用档位。
    """

    def __init__(self, parent=None, checked=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.boxes = {}
        for key in LIVE_RATIO_KEYS:
            cb = QCheckBox(LIVE_RATIO_NAMES[key], self)
            self.boxes[key] = cb
            lay.addWidget(cb)
        lay.addStretch(1)
        self.set_value(checked if checked is not None else None)

    def value(self):
        return {k: cb.isChecked() for k, cb in self.boxes.items()}

    def set_value(self, v):
        d = normalize_ratio(v)
        for k, cb in self.boxes.items():
            cb.setChecked(bool(d.get(k, True)))


class _Worker(QObject):
    """把耗时的网络查询放到后台线程，避免卡住界面。"""
    done = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception as e:  # noqa: BLE001
            result = e
        self.done.emit(result)


# ============================================================ 添加主播
# 对应 Frm_AddLiveRoom

class AddAnchorDialog(QDialog):
    def __init__(self, parent, client, cfg=None):
        super().__init__(parent)
        self.setWindowTitle("添加主播")
        self.setMinimumWidth(660)
        self.client = client
        self.cfg = dict(cfg or {})
        self.anchor = None
        self._worker = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "粘贴主播主页链接 / 直播间链接 / 抖音号 / sec_uid："))

        row = QHBoxLayout()
        self.txt_LiveUrl = QLineEdit(self)
        self.txt_LiveUrl.setPlaceholderText(
            "抖音号（如 XINMU__）  或  https://www.douyin.com/user/MS4wLjABAAAA…")
        self.txt_LiveUrl.returnPressed.connect(self._resolve)
        row.addWidget(self.txt_LiveUrl, 1)

        self.btn_AddPaste = QPushButton("粘贴", self)
        self.btn_AddPaste.clicked.connect(self._paste)
        row.addWidget(self.btn_AddPaste)

        self.btn_AddCler = QPushButton("清空", self)
        self.btn_AddCler.clicked.connect(lambda: self.txt_LiveUrl.clear())
        row.addWidget(self.btn_AddCler)

        self.btn_AddCopy = QPushButton("复制", self)
        self.btn_AddCopy.clicked.connect(self._copy)
        row.addWidget(self.btn_AddCopy)

        self.btn_StartAdd = QPushButton("识别", self)
        self.btn_StartAdd.clicked.connect(self._resolve)
        row.addWidget(self.btn_StartAdd)
        lay.addLayout(row)

        self.info = QTextEdit(self)
        self.info.setReadOnly(True)
        self.info.setMinimumHeight(170)
        self.info.setPlaceholderText(
            "支持以下任一形式：\n"
            "  · 抖音号            XINMU__\n"
            "  · 主播主页链接      https://www.douyin.com/user/MS4wLjABAAAA...\n"
            "  · 纯 sec_uid        MS4wLjABAAAA...\n\n"
            "说明：直播间链接（live.douyin.com/数字）目前受抖音风控限制，"
            "取不到主播信息，建议改用抖音号或主播主页链接。")
        lay.addWidget(self.info)

        # ---- 添加后默认（对应 ckb_AddDefaultMonitor / ckb_AddDefaultRec）
        box = QGroupBox("添加后默认", self)
        bl = QVBoxLayout(box)
        r1 = QHBoxLayout()
        self.ckb_AddDefaultMonitor = QCheckBox("加入监控", box)
        self.ckb_AddDefaultMonitor.setChecked(
            bool(self.cfg.get("添加时默认监控", True)))
        self.ckb_AddDefaultRec = QCheckBox("加入录制", box)
        self.ckb_AddDefaultRec.setChecked(
            bool(self.cfg.get("添加时默认录制", False)))
        r1.addWidget(self.ckb_AddDefaultMonitor)
        r1.addWidget(self.ckb_AddDefaultRec)
        r1.addStretch(1)
        bl.addLayout(r1)

        # ---- 清晰度（多选 + 优先顺序，对应 直播录制质量 字典）
        r1b = QHBoxLayout()
        r1b.addWidget(QLabel("清晰度（按此顺序取流）", box))
        self.cb_quality = QualityChecks(box, self.cfg.get("默认录制质量"))
        r1b.addWidget(self.cb_quality)
        r1b.addStretch(1)
        bl.addLayout(r1b)

        # ---- 分段录制（对应 ckb_IsSplitRec / nud_SplitRecNumMinute）
        r2 = QHBoxLayout()
        self.ckb_IsSplitRec = QCheckBox("分段录制，每段", box)
        self.ckb_IsSplitRec.setChecked(bool(self.cfg.get("是否分段录制")))
        self.ckb_IsSplitRec.toggled.connect(
            lambda v: self.nud_SplitRecNumMinute.setEnabled(v))
        self.nud_SplitRecNumMinute = QSpinBox(box)
        self.nud_SplitRecNumMinute.setRange(1, 720)
        self.nud_SplitRecNumMinute.setSuffix(" 分钟")
        self.nud_SplitRecNumMinute.setValue(
            int(self.cfg.get("分段录制几分钟", 30)))
        self.nud_SplitRecNumMinute.setEnabled(self.ckb_IsSplitRec.isChecked())
        r2.addWidget(self.ckb_IsSplitRec)
        r2.addWidget(self.nud_SplitRecNumMinute)

        r2.addSpacing(20)
        r2.addWidget(QLabel("录满", box))
        self.sp_stop = QSpinBox(box)
        self.sp_stop.setRange(0, 1440)
        self.sp_stop.setSuffix(" 分钟")
        self.sp_stop.setValue(int(self.cfg.get("录制多少分钟停止", 0)))
        r2.addWidget(self.sp_stop)
        r2.addWidget(QLabel("后停止（0=不限）", box))
        r2.addStretch(1)
        bl.addLayout(r2)
        lay.addWidget(box)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("添加")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    # -------------------------------------------------- 小工具
    def _paste(self):
        from PySide6.QtWidgets import QApplication
        self.txt_LiveUrl.setText(QApplication.clipboard().text().strip())
        self._resolve()

    def _copy(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.txt_LiveUrl.text())

    # -------------------------------------------------- 识别
    def _resolve(self):
        raw = self.txt_LiveUrl.text().strip()
        if not raw:
            self.info.setPlainText("请输入主播链接或抖音号")
            return
        self.btn_StartAdd.setEnabled(False)
        self.info.setPlainText("识别中…")
        self._start(lambda: self.client.resolve_input(raw), self._after_resolve)

    def _start(self, fn, slot):
        self._worker = _Worker(fn)
        self._worker.done.connect(slot, Qt.ConnectionType.QueuedConnection)
        threading.Thread(target=self._worker.run, daemon=True).start()

    def _after_resolve(self, res):
        self.btn_StartAdd.setEnabled(True)
        if isinstance(res, Exception):
            self.info.setPlainText("识别失败：%s" % res)
            return
        u = res.get("user_info") or {}
        sec = res.get("sec_uid") or ""

        a = Anchor(主播唯一码=sec)
        a.主播名称 = u.get("nickname") or ""
        a.粉丝量 = (u.get("mplatform_followers_count")
                  or u.get("follower_count") or "")
        a.点赞量 = u.get("total_favorited") or 0
        a.用户id = u.get("uid") or ""
        a.抖音号 = u.get("unique_id") or u.get("display_id") or ""
        a.房间号 = res.get("web_rid") or ""

        # 抖音号接口不带直播状态，用主页接口补一次
        live_extra = ""
        if res.get("kind") == DouyinLive.KIND_UID and sec:
            try:
                room, _u2 = self.client.live_of_user(sec)
                if room:
                    a.是否开播 = True
                    a.在线人数 = room.user_count
                    a.显示在线人数 = room.user_count_str or ""
                    a.直播标题 = room.title or ""
                    a.房间号 = room.web_rid or a.房间号
                    a.直播地址 = room.live_url or ""
                    live_extra = "  在线 %s" % (a.显示在线人数 or a.在线人数)
            except DouyinError:
                pass
        elif res.get("kind") == DouyinLive.KIND_SEC:
            try:
                room, _u2 = self.client.live_of_user(sec)
                if room:
                    a.是否开播 = True
                    a.在线人数 = room.user_count
                    a.显示在线人数 = room.user_count_str or ""
                    a.直播标题 = room.title or ""
                    a.房间号 = room.web_rid or a.房间号
                    a.直播地址 = room.live_url or ""
                    live_extra = "  在线 %s" % (a.显示在线人数 or a.在线人数)
            except DouyinError:
                pass

        self.anchor = a
        self.info.setPlainText(
            "识别成功（来源：%s）：\n"
            "  主播名称：%s\n"
            "  抖音号：%s\n"
            "  粉丝量：%s    获赞：%s\n"
            "  当前状态：%s%s\n"
            "  主播唯一码：%s\n"
            "  房间号：%s"
            % (res.get("kind") or "-",
               a.主播名称 or "-", a.抖音号 or "-",
               a.粉丝量 if a.粉丝量 != "" else "-",
               a.点赞量 or "-",
               "直播中" if a.是否开播 else "未开播", live_extra,
               a.主播唯一码 or "-", a.房间号 or "-"))

    def _ok(self):
        if not self.anchor or not self.anchor.主播唯一码:
            QMessageBox.warning(self, "提示", "请先点「识别」确认主播信息")
            return
        a = self.anchor
        a.是否监控 = self.ckb_AddDefaultMonitor.isChecked()
        a.是否录制 = self.ckb_AddDefaultRec.isChecked()
        a.直播录制质量 = self.cb_quality.value()
        a.是否分段录制 = self.ckb_IsSplitRec.isChecked()
        a.分段录制几分钟 = self.nud_SplitRecNumMinute.value()
        a.录制多少分钟停止 = self.sp_stop.value()
        a.是否开播提醒 = bool(self.cfg.get("开播提醒", True))
        a.是否关播提醒 = bool(self.cfg.get("关播提醒", True))
        a.是否开始录制提醒 = bool(self.cfg.get("开始录制提醒", True))
        a.是否停止录制提醒 = bool(self.cfg.get("停止录制提醒", True))
        a.是否在线人数提醒 = bool(self.cfg.get("在线人数提醒", False))
        a.添加时间 = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.accept()

    def exec_get_anchor(self):
        if self.exec() == QDialog.DialogCode.Accepted:
            return self.anchor
        return None


# ============================================================ 全局设置

class SettingsDialog(QDialog):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(580)
        self.cfg = dict(cfg)
        lay = QVBoxLayout(self)

        # ---- 监控
        g1 = QGroupBox("监控", self)
        f1 = QFormLayout(g1)
        self.sp_interval = QSpinBox(g1)
        self.sp_interval.setRange(5, 3600)
        self.sp_interval.setSingleStep(5)
        self.sp_interval.setSuffix(" 秒")
        self.sp_interval.setValue(int(self.cfg.get("监控间隔秒", 30)))
        f1.addRow("监控间隔", self.sp_interval)
        hint = QLabel("建议 ≥ 30 秒，过于频繁易触发风控", g1)
        hint.setStyleSheet("color:#888;")
        f1.addRow("", hint)
        self.ckb_auto = QCheckBox("启动软件时自动开启监控", g1)
        self.ckb_auto.setChecked(bool(self.cfg.get("启动时自动开启监控")))
        f1.addRow("", self.ckb_auto)
        self.ckb_tray = QCheckBox("最小化到托盘", g1)
        self.ckb_tray.setChecked(bool(self.cfg.get("窗口最小化到托盘")))
        f1.addRow("", self.ckb_tray)
        lay.addWidget(g1)

        # ---- 录制
        g2 = QGroupBox("录制", self)
        f2 = QFormLayout(g2)
        self.cb_quality = QualityChecks(g2, self.cfg.get("默认录制质量"))
        f2.addRow("默认录制质量", self.cb_quality)

        seg = QWidget(g2)
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(0, 0, 0, 0)
        self.cb_seg = QCheckBox("分段录制，每段", seg)
        self.cb_seg.setChecked(bool(self.cfg.get("是否分段录制")))
        self.sp_segmin = QSpinBox(seg)
        self.sp_segmin.setRange(1, 720)
        self.sp_segmin.setValue(int(self.cfg.get("分段录制几分钟", 30)))
        sl.addWidget(self.cb_seg)
        sl.addWidget(self.sp_segmin)
        sl.addWidget(QLabel("分钟", seg))
        sl.addStretch(1)
        f2.addRow("", seg)

        stop = QWidget(g2)
        stl = QHBoxLayout(stop)
        stl.setContentsMargins(0, 0, 0, 0)
        stl.addWidget(QLabel("默认录满", stop))
        self.sp_stop = QSpinBox(stop)
        self.sp_stop.setRange(0, 1440)
        self.sp_stop.setValue(int(self.cfg.get("录制多少分钟停止", 0)))
        stl.addWidget(self.sp_stop)
        stl.addWidget(QLabel("分钟后停止（0=不限制）", stop))
        stl.addStretch(1)
        f2.addRow("", stop)

        add = QWidget(g2)
        al = QHBoxLayout(add)
        al.setContentsMargins(0, 0, 0, 0)
        self.ckb_add_mon = QCheckBox("添加时默认加入监控", add)
        self.ckb_add_mon.setChecked(bool(self.cfg.get("添加时默认监控", True)))
        self.ckb_add_rec = QCheckBox("添加时默认加入录制", add)
        self.ckb_add_rec.setChecked(bool(self.cfg.get("添加时默认录制", False)))
        al.addWidget(self.ckb_add_mon)
        al.addWidget(self.ckb_add_rec)
        al.addStretch(1)
        f2.addRow("", add)
        lay.addWidget(g2)

        # ---- 提醒
        g3 = QGroupBox("提醒", self)
        gl = QVBoxLayout(g3)
        self.rem = {}
        keys = ["开播提醒", "关播提醒", "开始录制提醒",
                "停止录制提醒", "在线人数提醒"]
        row = QHBoxLayout()
        for i, key in enumerate(keys):
            cb = QCheckBox(key, g3)
            cb.setChecked(bool(self.cfg.get(key, True)))
            self.rem[key] = cb
            row.addWidget(cb)
            if i == 2:
                gl.addLayout(row)
                row = QHBoxLayout()
        row.addStretch(1)
        gl.addLayout(row)
        th = QWidget(g3)
        tl = QHBoxLayout(th)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel("在线人数阈值", th))
        self.sp_th = QSpinBox(th)
        self.sp_th.setRange(1, 100000000)
        self.sp_th.setSingleStep(1000)
        self.sp_th.setValue(int(self.cfg.get("在线人数提醒阈值", 10000)))
        tl.addWidget(self.sp_th)
        tl.addStretch(1)
        gl.addWidget(th)
        lay.addWidget(g3)

        # ---- 提示音
        g4 = QGroupBox("提示音", self)
        f4 = QFormLayout(g4)
        self.cb_sound = QComboBox(g4)
        self.cb_sound.addItems(["默认", "静音"])
        self.cb_sound.setCurrentText(self.cfg.get("提示音", "默认"))
        f4.addRow("模式", self.cb_sound)
        t = QLabel("把自定义 wav 放到 软件数据/提示音/ 即可替换\n"
                   "（开播提示音.wav、关播提示音.wav、开始录制提示音.wav、\n"
                   "  停止录制提示音.wav、在线人数提示音.wav）", g4)
        t.setStyleSheet("color:#888;")
        f4.addRow("", t)
        lay.addWidget(g4)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _ok(self):
        self.cfg.update({
            "监控间隔秒": self.sp_interval.value(),
            "启动时自动开启监控": self.ckb_auto.isChecked(),
            "窗口最小化到托盘": self.ckb_tray.isChecked(),
            "默认录制质量": self.cb_quality.value(),
            "是否分段录制": self.cb_seg.isChecked(),
            "分段录制几分钟": self.sp_segmin.value(),
            "录制多少分钟停止": self.sp_stop.value(),
            "添加时默认监控": self.ckb_add_mon.isChecked(),
            "添加时默认录制": self.ckb_add_rec.isChecked(),
            "在线人数提醒阈值": self.sp_th.value(),
            "提示音": self.cb_sound.currentText(),
        })
        for k, cb in self.rem.items():
            self.cfg[k] = cb.isChecked()
        self.accept()

    def exec_get_config(self):
        if self.exec() == QDialog.DialogCode.Accepted:
            return self.cfg
        return None


# ============================================================ 主播独立设置
# 对应 Frm_SetRoomInfo

class AnchorSettingsDialog(QDialog):
    def __init__(self, parent, anchor, cfg):
        super().__init__(parent)
        self.setWindowTitle("独立设置 - %s" % (anchor.主播名称 or "主播"))
        self.setMinimumWidth(560)
        self.anchor = anchor
        self.cfg = dict(cfg)
        lay = QVBoxLayout(self)

        g1 = QGroupBox("录制", self)
        f1 = QFormLayout(g1)
        row = QWidget(g1)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        self.cb_content = QComboBox(row)
        self.cb_content.addItems(RECORD_CONTENT_CHOICES)
        self.cb_content.setCurrentIndex(int(anchor.录制内容 or 0))
        rl.addWidget(self.cb_content)
        rl.addStretch(1)
        f1.addRow("录制内容", row)

        qrow = QWidget(g1)
        ql = QHBoxLayout(qrow)
        ql.setContentsMargins(0, 0, 0, 0)
        self.cb_quality = QualityChecks(qrow, anchor.直播录制质量)
        ql.addWidget(self.cb_quality)
        f1.addRow("清晰度（按此顺序取流）", qrow)

        stop = QWidget(g1)
        sl = QHBoxLayout(stop)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(QLabel("录满", stop))
        self.sp_stop = QSpinBox(stop)
        self.sp_stop.setRange(0, 1440)
        self.sp_stop.setValue(int(anchor.录制多少分钟停止 or 0))
        sl.addWidget(self.sp_stop)
        sl.addWidget(QLabel("分钟后停止（0 = 不限制）", stop))
        sl.addStretch(1)
        f1.addRow("", stop)

        # ---- 分段录制（对应 ckb_IsSplitRec / nud_SplitRecNumMinute）
        seg = QWidget(g1)
        gl2 = QHBoxLayout(seg)
        gl2.setContentsMargins(0, 0, 0, 0)
        self.ckb_IsSplitRec = QCheckBox("分段录制，每段", seg)
        self.ckb_IsSplitRec.setChecked(bool(anchor.是否分段录制))
        self.ckb_IsSplitRec.toggled.connect(
            lambda v: self.nud_SplitRecNumMinute.setEnabled(v))
        self.nud_SplitRecNumMinute = QSpinBox(seg)
        self.nud_SplitRecNumMinute.setRange(1, 720)
        self.nud_SplitRecNumMinute.setSuffix(" 分钟")
        self.nud_SplitRecNumMinute.setValue(int(anchor.分段录制几分钟 or 30))
        self.nud_SplitRecNumMinute.setEnabled(self.ckb_IsSplitRec.isChecked())
        gl2.addWidget(self.ckb_IsSplitRec)
        gl2.addWidget(self.nud_SplitRecNumMinute)
        gl2.addStretch(1)
        f1.addRow("", seg)
        lay.addWidget(g1)

        g2 = QGroupBox("提醒与记录", self)
        gl = QVBoxLayout(g2)
        self.vars = {}
        keys = [("是否开播提醒", "开播提醒"), ("是否关播提醒", "关播提醒"),
                ("是否开始录制提醒", "开始录制提醒"),
                ("是否停止录制提醒", "停止录制提醒"),
                ("是否在线人数提醒", "在线人数提醒"),
                ("是否讲解商品记录保存", "讲解商品记录"),
                ("是否购物车商品记录保存", "购物车商品记录"),
                ("是否登录状态访问直播间", "登录状态访问直播间"),
                ("是否转写话术文案", "语音转写文案")]
        row = None
        for i, (k, label) in enumerate(keys):
            if i % 3 == 0:
                row = QHBoxLayout()
                gl.addLayout(row)
            cb = QCheckBox(label, g2)
            cb.setChecked(bool(getattr(anchor, k)))
            self.vars[k] = cb
            row.addWidget(cb)
        row.addStretch(1)
        lay.addWidget(g2)

        g3 = QGroupBox("备注", self)
        f3 = QFormLayout(g3)
        self.ed_note = QLineEdit(anchor.备注 or "", g3)
        f3.addRow("", self.ed_note)
        lay.addWidget(g3)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _ok(self):
        a = self.anchor
        a.录制内容 = self.cb_content.currentIndex()
        a.直播录制质量 = self.cb_quality.value()
        a.录制多少分钟停止 = self.sp_stop.value()
        a.是否分段录制 = self.ckb_IsSplitRec.isChecked()
        a.分段录制几分钟 = self.nud_SplitRecNumMinute.value()
        for k, cb in self.vars.items():
            setattr(a, k, cb.isChecked())
        a.备注 = self.ed_note.text().strip()
        self.accept()

    def exec_get_anchor(self):
        if self.exec() == QDialog.DialogCode.Accepted:
            return self.anchor
        return None
