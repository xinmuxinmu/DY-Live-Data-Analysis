# -*- coding: utf-8 -*-
"""扩展工具窗口（PySide6）

对应原软件：
  UserC_PushMsgSet   -> PushSettingsDialog   推送消息设置
  UserC_VideoConve   -> ConvertDialog        视频转换
  Frm_GSDicLiveRatio -> StatsWindow          直播数据统计（QtCharts 原生图表）
  Frm_UseTutorial    -> TutorialWindow       使用教程
  Frm_ShowFlv        -> preview_stream       直播流预览（ffplay）
  批量导入            -> BatchImportDialog
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

from PySide6.QtCore import QMargins, QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton,
                               QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from dy import config, store
from dy.douyin import DouyinError, DouyinLive
from dy.models import QUALITY_CHOICES, RECORD_CONTENT_CHOICES, Anchor
from dy.pusher import PLATFORMS, Pusher
from dy.recorder import CONVERT_EXT, CONVERT_PRESETS, convert, media_info

from .dialogs import QualityChecks

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ============================================================ 批量导入

class _ImportWorker(QObject):
    progress = Signal(int, int)
    finished = Signal(list)


class BatchImportDialog(QDialog):
    """把一批链接 / 抖音号一次性导入。每行一个。"""

    def __init__(self, parent, client, cfg=None):
        super().__init__(parent)
        self.setWindowTitle("批量导入主播")
        self.resize(760, 640)
        self.client = client
        self.cfg = dict(cfg or {})
        self.anchors = []
        self._thread = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("每行一个：抖音号 或 主播主页链接 或 sec_uid"))
        hint = QLabel("（直播间链接 live.douyin.com/数字 当前受风控限制，"
                      "建议用抖音号或主页链接）")
        hint.setStyleSheet("color:#888;")
        lay.addWidget(hint)

        self.txt = QPlainTextEdit(self)
        self.txt.setFont(QFont("Consolas", 9))
        self.txt.setPlaceholderText(
            "XINMU__\n"
            "https://www.douyin.com/user/MS4wLjABAAAA...\n"
            "MS4wLjABAAAA...\n"
            "# 以 # 开头的行会被忽略")
        lay.addWidget(self.txt, 1)

        g = QGroupBox("导入选项", self)
        gl = QVBoxLayout(g)
        r1 = QHBoxLayout()
        self.cb_mon = QCheckBox("加入监控", g)
        self.cb_mon.setChecked(bool(self.cfg.get("添加时默认监控", True)))
        self.cb_rec = QCheckBox("加入录制", g)
        self.cb_rec.setChecked(bool(self.cfg.get("添加时默认录制", False)))
        self.cb_query = QCheckBox("联网核对主播信息（较慢）", g)
        self.cb_query.setChecked(True)
        r1.addWidget(self.cb_mon)
        r1.addWidget(self.cb_rec)
        r1.addWidget(self.cb_query)
        r1.addStretch(1)
        gl.addLayout(r1)

        r2 = QHBoxLayout()
        self.cb_seg = QCheckBox("分段录制，每段", g)
        self.cb_seg.setChecked(bool(self.cfg.get("是否分段录制")))
        self.cb_seg.toggled.connect(lambda v: self.sp_seg.setEnabled(v))
        self.sp_seg = QSpinBox(g)
        self.sp_seg.setRange(1, 720)
        self.sp_seg.setSuffix(" 分钟")
        self.sp_seg.setValue(int(self.cfg.get("分段录制几分钟", 30)))
        self.sp_seg.setEnabled(self.cb_seg.isChecked())
        r2.addWidget(self.cb_seg)
        r2.addWidget(self.sp_seg)
        r2.addStretch(1)
        gl.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(QLabel("清晰度（按此顺序取流）", g))
        self.cb_quality = QualityChecks(g, self.cfg.get("默认录制质量"))
        r3.addWidget(self.cb_quality)
        r3.addStretch(1)
        gl.addLayout(r3)
        lay.addWidget(g)

        self.pb = QProgressBar(self)
        self.pb.setVisible(False)
        lay.addWidget(self.pb)
        self.lbl = QLabel("")
        self.lbl.setStyleSheet("color:#0A7A0A;")
        lay.addWidget(self.lbl)

        row = QHBoxLayout()
        self.btn_file = QPushButton("从文件导入…", self)
        self.btn_file.clicked.connect(self._from_file)
        row.addWidget(self.btn_file)
        row.addStretch(1)
        lay.addLayout(row)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导入")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    def _from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择主播列表文件", config.DATA_DIR,
            "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                self.txt.setPlainText(f.read())
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "读取失败", str(e))

    def _ok(self):
        items, seen = [], set()
        for line in self.txt.toPlainText().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line not in seen:
                seen.add(line)
                items.append(line)
        if not items:
            QMessageBox.warning(self, "提示", "没有可导入的内容。")
            return

        self.btn_file.setEnabled(False)
        self.buttons.setEnabled(False)
        self.pb.setVisible(True)
        self.pb.setRange(0, len(items))
        self.pb.setValue(0)

        query = self.cb_query.isChecked()
        mon = self.cb_mon.isChecked()
        rec = self.cb_rec.isChecked()
        seg = self.cb_seg.isChecked()
        segmin = self.sp_seg.value()
        quality = self.cb_quality.value()

        worker = _ImportWorker(self)
        worker.progress.connect(self._on_progress,
                                Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_finished,
                                Qt.ConnectionType.QueuedConnection)
        self._worker = worker

        def run():
            out = []
            for i, raw in enumerate(items, 1):
                worker.progress.emit(i, len(items))
                a = Anchor(是否监控=mon, 是否录制=rec,
                           是否分段录制=seg, 分段录制几分钟=segmin,
                           直播录制质量=quality,
                           录制多少分钟停止=int(
                               self.cfg.get("录制多少分钟停止", 0) or 0))
                a.添加时间 = time.strftime("%Y-%m-%dT%H:%M:%S")
                if query:
                    # 统一解析：抖音号 / 主页链接 / sec_uid
                    try:
                        res = self.client.resolve_input(raw)
                    except DouyinError as e:
                        a.错误 = str(e)
                        out.append(a)
                        continue
                    u = res.get("user_info") or {}
                    a.主播唯一码 = res.get("sec_uid") or ""
                    a.主播名称 = u.get("nickname") or ""
                    a.粉丝量 = (u.get("mplatform_followers_count")
                              or u.get("follower_count") or "")
                    a.点赞量 = u.get("total_favorited") or 0
                    a.用户id = u.get("uid") or ""
                    a.抖音号 = u.get("unique_id") or u.get("display_id") or ""
                    a.房间号 = res.get("web_rid") or ""
                    try:
                        room, _u2 = self.client.live_of_user(a.主播唯一码)
                    except DouyinError:
                        room = None
                    if room:
                        a.是否开播 = True
                        a.在线人数 = room.user_count
                        a.显示在线人数 = room.user_count_str or ""
                        a.直播标题 = room.title or ""
                        a.房间号 = room.web_rid or a.房间号
                        a.直播地址 = room.live_url or ""
                    self.client.sleep()
                else:
                    sec = DouyinLive.parse_sec_uid(raw)
                    a.主播唯一码 = sec or raw
                if a.主播唯一码:
                    out.append(a)
            worker.finished.emit(out)

        threading.Thread(target=run, daemon=True).start()

    def _on_progress(self, i, n):
        self.pb.setValue(i)
        self.lbl.setText("正在处理 %d/%d…" % (i, n))

    def _on_finished(self, anchors):
        self.anchors = anchors
        self.accept()

    def exec_get_anchors(self):
        if self.exec() == QDialog.DialogCode.Accepted:
            return self.anchors
        return None


# ============================================================ 推送设置

class PushSettingsDialog(QDialog):
    test_done = Signal(bool, str)

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("推送消息设置")
        self.setMinimumWidth(660)
        self.test_done.connect(self._on_test_done,
                               Qt.ConnectionType.QueuedConnection)
        self.cfg = dict(cfg)
        p = dict(self.cfg.get("推送") or {})
        ev = dict(p.get("事件") or {})

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("把开播/关播等事件推送到群机器人（可选）"))
        hint = QLabel("支持企业微信 / 钉钉 / 飞书 群机器人，或任意自定义 Webhook")
        hint.setStyleSheet("color:#888;")
        lay.addWidget(hint)

        self.cb_on = QCheckBox("启用推送", self)
        self.cb_on.setChecked(bool(p.get("启用")))
        lay.addWidget(self.cb_on)

        g = QGroupBox("连接", self)
        f = QFormLayout(g)
        self.cb_plat = QComboBox(g)
        self.cb_plat.addItems(PLATFORMS)
        self.cb_plat.setCurrentText(p.get("平台") or PLATFORMS[0])
        f.addRow("平台", self.cb_plat)
        self.ed_url = QLineEdit(p.get("Webhook") or "", g)
        self.ed_url.setPlaceholderText(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…")
        f.addRow("Webhook 地址", self.ed_url)
        self.ed_pre = QLineEdit(p.get("标题前缀") or "", g)
        self.ed_pre.setPlaceholderText("可留空")
        f.addRow("标题前缀", self.ed_pre)
        lay.addWidget(g)

        g2 = QGroupBox("推送哪些事件", self)
        gl = QVBoxLayout(g2)
        row = QHBoxLayout()
        self.ev = {}
        keys = ["开播", "关播", "开始录制", "停止录制", "在线人数"]
        for i, k in enumerate(keys):
            cb = QCheckBox(k, g2)
            cb.setChecked(bool(ev.get(k, k != "在线人数")))
            self.ev[k] = cb
            row.addWidget(cb)
            if i == 2:
                gl.addLayout(row)
                row = QHBoxLayout()
        row.addStretch(1)
        gl.addLayout(row)
        lay.addWidget(g2)

        row2 = QHBoxLayout()
        self.btn_test = QPushButton("发送测试消息", self)
        self.btn_test.clicked.connect(self._test)
        row2.addWidget(self.btn_test)
        self.lbl = QLabel("")
        self.lbl.setStyleSheet("color:#888;")
        row2.addWidget(self.lbl, 1)
        lay.addLayout(row2)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _collect(self):
        return {
            "启用": self.cb_on.isChecked(),
            "平台": self.cb_plat.currentText(),
            "Webhook": self.ed_url.text().strip(),
            "标题前缀": self.ed_pre.text().strip(),
            "事件": {k: cb.isChecked() for k, cb in self.ev.items()},
        }

    def _test(self):
        conf = self._collect()
        if not conf["Webhook"]:
            self.lbl.setText("请先填写 Webhook 地址")
            self.lbl.setStyleSheet("color:#C00000;")
            return
        self.lbl.setText("正在发送…")
        self.lbl.setStyleSheet("color:#888;")
        self.btn_test.setEnabled(False)

        def run():
            ok, msg = Pusher(lambda: {"推送": conf}).test()
            self.test_done.emit(ok, str(msg)[:70])

        threading.Thread(target=run, daemon=True).start()

    def _on_test_done(self, ok, msg):
        self.lbl.setText(msg)
        self.lbl.setStyleSheet("color:#0A7A0A;" if ok else "color:#C00000;")
        self.btn_test.setEnabled(True)

    def _ok(self):
        self.cfg["推送"] = self._collect()
        self.accept()

    def exec_get_config(self):
        if self.exec() == QDialog.DialogCode.Accepted:
            return self.cfg
        return None


# ============================================================ 视频转换

class ConvertDialog(QDialog):
    convert_done = Signal(bool, str, str)

    def __init__(self, parent, cfg=None):
        super().__init__(parent)
        self.setWindowTitle("视频转码 / 压缩")
        self.setMinimumWidth(680)
        self.cfg = dict(cfg or {})
        self.convert_done.connect(self._on_done,
                                  Qt.ConnectionType.QueuedConnection)
        self._busy = False

        lay = QVBoxLayout(self)

        row = QHBoxLayout()
        self.ed_src = QLineEdit(self)
        self.ed_src.setPlaceholderText("选择要转换的视频/音频文件")
        row.addWidget(self.ed_src, 1)
        btn = QPushButton("浏览…", self)
        btn.clicked.connect(self._pick)
        row.addWidget(btn)
        lay.addLayout(row)

        f = QFormLayout()
        self.cb_preset = QComboBox(self)
        self.cb_preset.addItems(list(CONVERT_PRESETS))
        f.addRow("转换方式", self.cb_preset)
        lay.addLayout(f)

        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl_info)

        self.pb = QProgressBar(self)
        self.pb.setRange(0, 0)
        self.pb.setVisible(False)
        lay.addWidget(self.pb)
        self.lbl = QLabel("")
        lay.addWidget(self.lbl)

        btns = QDialogButtonBox(self)
        self.btn_go = btns.addButton("开始转换",
                                     QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_go.clicked.connect(self._run)
        btns.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole
                       ).clicked.connect(self.reject)
        lay.addWidget(btns)

    def _pick(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择视频/音频文件", config.VIDEO_DIR,
            "媒体文件 (*.mp4 *.flv *.mkv *.m4a *.mp3 *.ts *.avi);;所有文件 (*.*)")
        if not path:
            return
        self.ed_src.setText(path)
        info = media_info(path)
        if info:
            fmt = info.get("format") or {}
            dur = float(fmt.get("duration") or 0)
            size = int(fmt.get("size") or 0) / 1048576.0
            vs = [s for s in (info.get("streams") or [])
                  if s.get("codec_type") == "video"]
            wh = ("%sx%s" % (vs[0].get("width"), vs[0].get("height"))
                  if vs else "-")
            self.lbl_info.setText(
                "时长 %s ｜ 分辨率 %s ｜ 大小 %.1f MB"
                % (time.strftime("%H:%M:%S", time.gmtime(dur)), wh, size))
        else:
            self.lbl_info.setText("（未取到媒体信息，仍可转换）")

    def _run(self):
        if self._busy:
            return
        src = self.ed_src.text().strip()
        if not src or not os.path.isfile(src):
            QMessageBox.warning(self, "提示", "请先选择有效的源文件")
            return
        if not config.FFMPEG:
            QMessageBox.critical(
                self, "缺少 ffmpeg",
                "未找到 ffmpeg，无法转换。\n"
                "请把 ffmpeg 文件夹放到程序目录下。")
            return
        preset = self.cb_preset.currentText()
        ext = CONVERT_EXT.get(preset, os.path.splitext(src)[1] or ".mp4")
        base = os.path.splitext(os.path.basename(src))[0]
        out = os.path.join(os.path.dirname(src), "%s_转换%s" % (base, ext))
        n = 1
        while os.path.exists(out):
            out = os.path.join(os.path.dirname(src),
                               "%s_转换(%d)%s" % (base, n, ext))
            n += 1

        self._busy = True
        self.btn_go.setEnabled(False)
        self.pb.setVisible(True)
        self.lbl.setText("转换中…（大文件可能耗时较久）")
        self.lbl.setStyleSheet("color:#888;")

        def run():
            ok, msg = convert(src, out, preset)
            self.convert_done.emit(ok, str(msg), out)

        threading.Thread(target=run, daemon=True).start()

    def _on_done(self, ok, msg, out):
        self._busy = False
        self.pb.setVisible(False)
        self.btn_go.setEnabled(True)
        if ok:
            self.lbl.setText("完成：%s" % out)
            self.lbl.setStyleSheet("color:#0A7A0A;")
            if QMessageBox.question(
                    self, "转换完成",
                    "已生成：\n%s\n\n是否打开所在目录？" % out,
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
            ) == QMessageBox.StandardButton.Yes:
                os.startfile(os.path.dirname(out))  # noqa: S606
        else:
            self.lbl.setText("失败")
            self.lbl.setStyleSheet("color:#C00000;")
            QMessageBox.critical(self, "转换失败", str(msg)[-600:])


# ============================================================ 录制设置
# 对应原软件 Frm_Main 的 tab_LuZhiSet（录制设置 / 录制目录 / 转码）

class RecordSettingsDialog(QDialog):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("录制设置")
        self.setMinimumWidth(640)
        self.cfg = dict(cfg)
        lay = QVBoxLayout(self)

        # ---- 录制设置（对应 tabPage_luzhicgset）
        g1 = QGroupBox("录制设置", self)
        f1 = QFormLayout(g1)
        self.cb_quality = QualityChecks(g1, self.cfg.get("默认录制质量"))
        f1.addRow("默认录制清晰度", self.cb_quality)

        self.cb_content = QComboBox(g1)
        self.cb_content.addItems(RECORD_CONTENT_CHOICES)
        self.cb_content.setCurrentIndex(
            int(self.cfg.get("默认录制内容", 0) or 0))
        f1.addRow("默认录制内容", self.cb_content)

        # ---- 录制方式（原软件 Frm_Main 的 rdbtn_录制流 / rdbtn_下载流）
        mw = QWidget(g1)
        ml = QHBoxLayout(mw)
        ml.setContentsMargins(0, 0, 0, 0)
        self.rb_rec = QRadioButton("录制流", mw)
        self.rb_dl = QRadioButton("下载流", mw)
        mode = self.cfg.get("录制方式") or "录制流"
        self.rb_dl.setChecked(mode == "下载流")
        self.rb_rec.setChecked(mode != "下载流")
        self.rb_rec.setToolTip(
            "使用 ffmpeg 进行直播录制。\n"
            "保存的是 ts 格式，有播放器可播放。")
        self.rb_dl.setToolTip(
            "使用软件下载方式（直接下载 FLV 流，断线自动续传）。\n"
            "保存的是 FLV 格式，无法播放需要转码。")
        ml.addWidget(self.rb_rec)
        ml.addWidget(self.rb_dl)
        ml.addStretch(1)
        f1.addRow("录制方式", mw)
        self.lb_mode_hint = QLabel(
            "两者区别：录制保存 ts（可直接播放）；下载保存 FLV（需转码）。\n"
            "如果选择录制无法录制，就选下载方式；反之亦然。", g1)
        self.lb_mode_hint.setStyleSheet("color:#808080;")
        f1.addRow("", self.lb_mode_hint)

        seg = QWidget(g1)
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(0, 0, 0, 0)
        self.cb_seg = QCheckBox("分段录制，每段", seg)
        self.cb_seg.setChecked(bool(self.cfg.get("是否分段录制")))
        self.cb_seg.toggled.connect(lambda v: self.sp_seg.setEnabled(v))
        self.sp_seg = QSpinBox(seg)
        self.sp_seg.setRange(1, 720)
        self.sp_seg.setSuffix(" 分钟")
        self.sp_seg.setValue(int(self.cfg.get("分段录制几分钟", 30)))
        self.sp_seg.setEnabled(self.cb_seg.isChecked())
        sl.addWidget(self.cb_seg)
        sl.addWidget(self.sp_seg)
        sl.addStretch(1)
        f1.addRow("", seg)

        stop = QWidget(g1)
        stl = QHBoxLayout(stop)
        stl.setContentsMargins(0, 0, 0, 0)
        stl.addWidget(QLabel("默认录满", stop))
        self.sp_stop = QSpinBox(stop)
        self.sp_stop.setRange(0, 1440)
        self.sp_stop.setValue(int(self.cfg.get("录制多少分钟停止", 0)))
        stl.addWidget(self.sp_stop)
        stl.addWidget(QLabel("分钟后自动停止（0=不限制）", stop))
        stl.addStretch(1)
        f1.addRow("", stop)
        lay.addWidget(g1)

        # ---- 录制目录（对应 tabPage_luzhimulu）
        g2 = QGroupBox("录制目录", self)
        f2 = QFormLayout(g2)
        row = QWidget(g2)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        self.ed_dir = QLineEdit(
            self.cfg.get("录制目录") or config.VIDEO_DIR, row)
        rl.addWidget(self.ed_dir, 1)
        b1 = QPushButton("选择…", row)
        b1.clicked.connect(self._pick_dir)
        rl.addWidget(b1)
        b2 = QPushButton("打开", row)
        b2.clicked.connect(
            lambda: self._open(self.ed_dir.text().strip()))
        rl.addWidget(b2)
        f2.addRow("保存目录", row)
        b3 = QPushButton("初始化录制目录（按主播名建子目录）", g2)
        b3.clicked.connect(self._init_dir)
        f2.addRow("", b3)
        hint = QLabel("录制文件保存在 <保存目录>/<主播名>/ 下", g2)
        hint.setStyleSheet("color:#888;")
        f2.addRow("", hint)
        lay.addWidget(g2)

        # ---- 转码设置（对应「录制完成后无损转码MP4」）
        g3 = QGroupBox("转码设置", self)
        gl = QVBoxLayout(g3)
        self.cb_auto_mp4 = QCheckBox("录制完成后无损转码 MP4（-c copy）", g3)
        self.cb_auto_mp4.setChecked(bool(self.cfg.get("录制完成后无损转码MP4")))
        gl.addWidget(self.cb_auto_mp4)
        self.cb_del_src = QCheckBox("转码完成后删除源文件", g3)
        self.cb_del_src.setChecked(bool(self.cfg.get("转码完成后删除源文件")))
        gl.addWidget(self.cb_del_src)
        f3 = QFormLayout()
        self.cb_preset = QComboBox(g3)
        self.cb_preset.addItems(list(CONVERT_PRESETS))
        self.cb_preset.setCurrentText(
            self.cfg.get("默认转码预设") or list(CONVERT_PRESETS)[0])
        f3.addRow("默认转码方式", self.cb_preset)
        gl.addLayout(f3)
        lay.addWidget(g3)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _pick_dir(self):
        p = QFileDialog.getExistingDirectory(
            self, "选择录制保存目录", self.ed_dir.text().strip()
            or config.VIDEO_DIR)
        if p:
            self.ed_dir.setText(p)

    @staticmethod
    def _open(p):
        if p and os.path.isdir(p):
            os.startfile(p)  # noqa: S606

    def _init_dir(self):
        p = self.ed_dir.text().strip()
        if not p:
            return
        os.makedirs(p, exist_ok=True)
        QMessageBox.information(self, "完成", "已创建：%s" % p)

    def _ok(self):
        self.cfg.update({
            "默认录制质量": self.cb_quality.value(),
            "默认录制内容": self.cb_content.currentIndex(),
            "录制方式": ("下载流" if self.rb_dl.isChecked() else "录制流"),
            "是否分段录制": self.cb_seg.isChecked(),
            "分段录制几分钟": self.sp_seg.value(),
            "录制多少分钟停止": self.sp_stop.value(),
            "录制目录": self.ed_dir.text().strip() or config.VIDEO_DIR,
            "录制完成后无损转码MP4": self.cb_auto_mp4.isChecked(),
            "转码完成后删除源文件": self.cb_del_src.isChecked(),
            "默认转码预设": self.cb_preset.currentText(),
        })
        self.accept()

    def exec_get_config(self):
        if self.exec() == QDialog.DialogCode.Accepted:
            return self.cfg
        return None


# ============================================================ 浏览器设置

class BrowserSettingsDialog(QDialog):
    """对应原软件设置页里的浏览器相关项
    （内置浏览器 / 缓存与登录信息 / 在浏览器打开观看）。"""

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("浏览器设置")
        self.setMinimumWidth(620)
        self.cfg = dict(cfg)
        lay = QVBoxLayout(self)

        g = QGroupBox("打开方式", self)
        gl = QVBoxLayout(g)
        self.rb_outer = QRadioButton("使用系统默认浏览器打开直播间", g)
        self.rb_inner = QRadioButton("使用程序内置浏览器打开（实验性）", g)
        if self.cfg.get("使用内置浏览器"):
            self.rb_inner.setChecked(True)
        else:
            self.rb_outer.setChecked(True)
        gl.addWidget(self.rb_outer)
        gl.addWidget(self.rb_inner)
        hint = QLabel("本版本未内嵌 Chromium，选「内置浏览器」时仍会调用系统浏览器，"
                      "但会按下方目录保存浏览数据。", g)
        hint.setStyleSheet("color:#888;")
        hint.setWordWrap(True)
        gl.addWidget(hint)
        lay.addWidget(g)

        g2 = QGroupBox("浏览器数据", self)
        f2 = QFormLayout(g2)
        row = QWidget(g2)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        self.ed_dir = QLineEdit(
            self.cfg.get("浏览器用户数据目录")
            or os.path.join(config.DATA_DIR, "浏览器用户数据"), row)
        rl.addWidget(self.ed_dir, 1)
        b1 = QPushButton("选择…", row)
        b1.clicked.connect(self._pick)
        rl.addWidget(b1)
        b2 = QPushButton("打开", row)
        b2.clicked.connect(self._open_dir)
        rl.addWidget(b2)
        f2.addRow("用户数据目录", row)
        lay.addWidget(g2)

        g3 = QGroupBox("维护", self)
        gl3 = QVBoxLayout(g3)
        b3 = QPushButton("删除浏览器缓存与登录信息", g3)
        b3.clicked.connect(self._clear)
        gl3.addWidget(b3)
        t = QLabel("删除后需要重新登录抖音。目录内只清理缓存类子目录，"
                   "不会删除主播数据。", g3)
        t.setStyleSheet("color:#888;")
        t.setWordWrap(True)
        gl3.addWidget(t)
        lay.addWidget(g3)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _pick(self):
        p = QFileDialog.getExistingDirectory(
            self, "选择浏览器用户数据目录", self.ed_dir.text().strip())
        if p:
            self.ed_dir.setText(p)

    def _open_dir(self):
        p = self.ed_dir.text().strip()
        if p:
            os.makedirs(p, exist_ok=True)
            os.startfile(p)  # noqa: S606

    def _clear(self):
        p = self.ed_dir.text().strip()
        if not p or not os.path.isdir(p):
            QMessageBox.information(self, "提示", "目录不存在，无需清理")
            return
        if QMessageBox.question(
                self, "确认", "删除「%s」下的缓存与登录信息？" % p,
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        import shutil
        removed = 0
        for name in ("Cache", "Code Cache", "GPUCache", "Cookies",
                     "Cookies-journal", "Login Data", "Web Data",
                     "Local Storage", "Session Storage"):
            target = os.path.join(p, name)
            try:
                if os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
                    removed += 1
                elif os.path.isfile(target):
                    os.remove(target)
                    removed += 1
            except OSError:
                pass
        QMessageBox.information(self, "完成", "已清理 %d 项" % removed)

    def _ok(self):
        self.cfg.update({
            "使用内置浏览器": self.rb_inner.isChecked(),
            "浏览器用户数据目录": self.ed_dir.text().strip()
            or os.path.join(config.DATA_DIR, "浏览器用户数据"),
        })
        self.accept()

    def exec_get_config(self):
        if self.exec() == QDialog.DialogCode.Accepted:
            return self.cfg
        return None


# ============================================================ 数据统计

class StatsWindow(QWidget):
    """采集记录统计：概览 + 各主播最高在线柱状图（QtCharts）+ 记录明细。"""

    def __init__(self, parent, anchors):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("直播数据统计")
        self.resize(1060, 720)
        self.anchors = list(anchors or [])

        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("统计日期"))
        self.cb_day = QComboBox(self)
        days = store.list_record_days() or [time.strftime("%Y-%m-%d")]
        self.cb_day.addItems(days)
        self.cb_day.currentTextChanged.connect(lambda _t: self.refresh())
        top.addWidget(self.cb_day)
        btn = QPushButton("刷新", self)
        btn.clicked.connect(self.refresh)
        top.addWidget(btn)
        top.addStretch(1)
        btn_exp = QPushButton("导出 Excel", self)
        btn_exp.clicked.connect(self._export)
        top.addWidget(btn_exp)
        lay.addLayout(top)

        self.summary = QLabel("")
        lay.addWidget(self.summary)

        # ---- 图表
        g = QGroupBox("各主播最高在线人数", self)
        gl = QVBoxLayout(g)
        self._chart_holder = gl
        self._chart_view = None
        self._chart_ph = QLabel("加载中…", g)
        self._chart_ph.setStyleSheet("color:#888;")
        gl.addWidget(self._chart_ph)
        lay.addWidget(g, 2)

        # ---- 明细
        g2 = QGroupBox("采集记录明细", self)
        g2l = QVBoxLayout(g2)
        self.table = QTableWidget(0, 9, g2)
        self.table.setHorizontalHeaderLabels(
            ["记录时间", "主播名称", "直播标题", "粉丝量", "点赞量",
             "在线人数", "最高在线人数", "场观总人数", "直播状态"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, w in enumerate([150, 160, 240, 90, 90, 90, 100, 100, 80]):
            self.table.setColumnWidth(i, w)
        g2l.addWidget(self.table)
        lay.addWidget(g2, 3)

        self.refresh()

    # -------------------------------------------------- 刷新
    def refresh(self):
        day = self.cb_day.currentText()
        snaps = store.load_snapshots(day)
        n_mon = sum(1 for a in self.anchors if a.是否监控)
        n_rec = sum(1 for a in self.anchors if a.是否录制)
        self.summary.setText(
            "主播 %d 个（监控 %d，录制 %d）　|　%s 采集记录 %d 条"
            % (len(self.anchors), n_mon, n_rec, day, len(snaps)))

        self.table.setRowCount(0)
        for s in snaps:
            r = self.table.rowCount()
            self.table.insertRow(r)
            vals = [s.记录时间 or s.采集时间, s.主播名称, s.直播标题,
                    s.粉丝量, s.点赞量, s.在线人数, s.最高在线人数,
                    s.场观总人数, s.直播状态]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(
                    "" if v is None else str(v)))

        data = {}
        for s in snaps:
            try:
                v = int(str(s.在线人数).replace(",", "") or 0)
            except (TypeError, ValueError):
                v = 0
            if s.主播名称:
                data[s.主播名称] = max(data.get(s.主播名称, 0), v)
        if not data:
            for a in self.anchors:
                try:
                    v = int(a.最高在线人数 or a.在线人数 or 0)
                except (TypeError, ValueError):
                    v = 0
                if a.主播名称:
                    data[a.主播名称] = max(data.get(a.主播名称, 0), v)
        self._draw_chart(data)

    def _draw_chart(self, data):
        try:
            from PySide6.QtCharts import (QBarCategoryAxis, QBarSeries, QBarSet,
                                          QChart, QChartView, QValueAxis)
        except Exception:  # noqa: BLE001
            self._chart_ph.setText("未安装 QtCharts，无法显示图表")
            return

        # 清掉旧图与占位标签
        # 注意：仅 removeWidget + deleteLater 不够——deleteLater 要等事件循环
        # 处理 DeferredDelete 才生效，期间旧控件仍然可见。必须先 setParent(None)。
        for attr in ("_chart_view", "_chart_ph"):
            w = getattr(self, attr, None)
            if w is not None:
                self._chart_holder.removeWidget(w)
                w.setParent(None)
                w.deleteLater()
                setattr(self, attr, None)

        chart = QChart()
        chart.setMargins(QMargins(6, 6, 6, 6))

        if not data:
            chart.setTitle("暂无数据")
            view = QChartView(chart, self)
            view.setMinimumHeight(210)
            self._chart_holder.addWidget(view)
            self._chart_view = view
            return

        items = sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:12]
        bs = QBarSet("最高在线")
        bs.setColor(QColor("#E53935"))
        bs.setBorderColor(QColor("#C62828"))
        bs.append([v for _n, v in items])
        series = QBarSeries()
        series.append(bs)
        chart.addSeries(series)

        ax = QBarCategoryAxis()
        ax.append([(n if len(n) <= 8 else n[:7] + "…") for n, _v in items])
        chart.addAxis(ax, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(ax)

        ay = QValueAxis()
        top = max(v for _n, v in items) or 1
        ay.setRange(0, top * 1.15)
        ay.setLabelFormat("%d")
        ay.applyNiceNumbers()
        chart.addAxis(ay, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(ay)

        chart.legend().setVisible(False)
        chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)

        view = QChartView(chart, self)
        view.setMinimumHeight(230)
        self._chart_holder.addWidget(view)
        self._chart_view = view

    def _export(self):
        from dy.models import SNAPSHOT_COLUMNS
        from dy.xlsx import write_xlsx
        day = self.cb_day.currentText()
        snaps = store.load_snapshots(day)
        if not snaps:
            QMessageBox.information(self, "提示", "%s 没有采集记录" % day)
            return
        default = os.path.join(config.DATA_DIR, "采集记录_%s.xlsx" % day)
        path, _ = QFileDialog.getSaveFileName(
            self, "导出采集记录", default, "Excel 工作簿 (*.xlsx)")
        if not path:
            return
        rows = [s.to_row(i) for i, s in enumerate(snaps, start=1)]
        write_xlsx(path, [("采集记录", SNAPSHOT_COLUMNS, rows)])
        if QMessageBox.question(
                self, "导出完成", "已导出到：\n%s\n\n是否打开？" % path,
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            os.startfile(path)  # noqa: S606


# ============================================================ 使用教程

TUTORIAL = """\
DY直播数据分析（Python 版）使用教程
════════════════════════════════════════════════════

一、快速上手
  1. 点「＋ 添加主播」，粘贴主播主页链接（https://www.douyin.com/user/MS4wLjAB…），
     点「识别」确认信息后「添加」。
  2. 在主列表里双击「监控」「录制」两列切换开关。
  3. 点「▶ 开启监控录制」，程序就会按设定间隔轮询，开播自动录制。

二、添加主播的三种方式
  · 单个添加：工具栏「＋ 添加主播」（Ctrl+N）
  · 批量导入：工具栏「批量导入」（Ctrl+I），每行一个链接，可从文件读取
  · 热门直播：工具栏「热门直播」（F7），多选后点「加入监控」

  提示：直播间链接（live.douyin.com/数字）目前受抖音风控限制，取不到主播
  信息，请改用主播主页链接。

三、监控与录制
  · 监控间隔在「设置」里调整，建议 ≥ 30 秒，过于频繁易触发风控。
  · 录制质量：原画 / 超清 / 高清 / 标清 / 流畅。程序按清晰度选择对应码率的
    流，直存不转码（CPU 占用最低）。
  · 分段录制、录满 N 分钟停止，在右键「独立设置功能」里按主播单独配置。
  · 录制文件保存在 软件数据/录制文件/<主播名>/ 下。

四、提醒与推送
  · 提示音：把自定义 wav 放进 软件数据/提示音/，文件名见「设置 → 提示音」。
  · 群机器人推送：「工具 → 推送消息设置」，支持企业微信 / 钉钉 / 飞书
    群机器人，可推送开播、关播、开始/停止录制、在线人数达标等事件。

五、数据与导出
  · 每次检测到开播会自动写入一条采集记录
    （软件数据/记录数据/日期_直播数据.csv）。
  · 主列表数据：工具栏「导出 Excel」（Ctrl+E）。
  · 采集记录统计：「工具 → 直播数据统计」（F8），可看柱状图并导出 Excel。

六、其他工具
  · 视频转换：「工具 → 视频转换 / 压缩」，把录好的文件压缩或转成 MP3。
  · 直播流预览：右键「预览直播流」，用 ffplay 直接播放当前流地址。
  · 运行日志：「工具 → 运行日志」，排查问题时看这里。

七、常见问题
  Q：提示「接口返回空响应（被风控拦截）」？
  A：抖音对高频请求有限制。请调大监控间隔、减少主播数量、稍后重试。
  Q：录制失败，提示未取到直播流地址？
  A：部分直播间不对外提供流地址（如仅粉丝可见、付费直播）。
  Q：为什么「直播标题」列有时是空的？
  A：主播主页接口不返回直播标题，只有被风控的直播间详情接口才返回。
     从「热门直播」加入的主播会有标题。
  Q：未找到 ffmpeg？
  A：把 ffmpeg 文件夹放到程序目录下（需含 ffmpeg.exe / ffplay.exe /
     ffprobe.exe 及同目录 DLL）。

════════════════════════════════════════════════════
"""


class TutorialWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("使用教程")
        self.resize(840, 660)
        lay = QVBoxLayout(self)
        txt = QPlainTextEdit(self)
        txt.setReadOnly(True)
        txt.setFont(QFont("Microsoft YaHei", 10))
        txt.setPlainText(TUTORIAL)
        lay.addWidget(txt)


def preview_stream(parent, url):
    """用 ffplay 打开直播流预览窗口（对应原软件 Frm_ShowFlv）。"""
    if not url:
        QMessageBox.information(parent, "提示", "该主播暂无可用流地址（可能未开播）")
        return
    if not config.FFPLAY:
        QMessageBox.critical(
            parent, "缺少 ffplay",
            "未找到 ffplay，无法预览。\n"
            "请把 ffmpeg 文件夹（含 ffplay.exe）放到程序目录下。")
        return
    try:
        subprocess.Popen(
            [config.FFPLAY, "-window_title", "直播流预览",
             "-loglevel", "warning", url],
            creationflags=CREATE_NO_WINDOW)
    except Exception as e:  # noqa: BLE001
        QMessageBox.critical(parent, "预览失败", str(e))
