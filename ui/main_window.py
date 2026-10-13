# -*- coding: utf-8 -*-
"""主窗口（PySide6 / Qt Widgets）

对应原软件 Frm_Main。

采用 Model/View（QAbstractTableModel + QTableView）而不是 QTableWidget：
监控每秒刷新「监控时间 / 已录时间 / 在线人数」，用 model 只发 dataChanged，
不会像 QTableWidget 那样重建整表、丢失选中项与滚动位置。

列清单与右键菜单项均取自原程序 DY直播数据分析.core.exe 的内嵌字符串
（列名 @5026746 起、右键菜单 @5040345 起），未做臆测。
"""
from __future__ import annotations

import os
import subprocess
import time
import webbrowser

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QObject, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog,
                               QDialogButtonBox, QFileDialog, QHeaderView,
                               QInputDialog, QLabel,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QStatusBar, QTableView,
                               QToolBar, QVBoxLayout, QWidget)

from dy import config, store
from dy.douyin import DouyinError, DouyinLive
from dy.models import (SNAPSHOT_COLUMNS, Anchor, snapshot_from_room,
                       enabled_ratios, normalize_ratio, primary_ratio,
                       LIVE_RATIO_KEYS, LIVE_RATIO_NAMES)
from dy.monitor import Monitor
from dy.xlsx import write_xlsx

from .dialogs import AddAnchorDialog, AnchorSettingsDialog, SettingsDialog
from .tools import (BatchImportDialog, BrowserSettingsDialog, ConvertDialog,
                    PushSettingsDialog, RecordSettingsDialog, StatsWindow,
                    TutorialWindow, preview_stream)

A = Qt.AlignmentFlag

# ---------------------------------------------------------------- 列定义
# 列名与顺序完全取自 core.exe 字符串堆（偏移 5026745 起，主列表
# RefreshRoomListAutoColumns 的列头清单），逐项对齐，未做增删：
#   直播状态 / 监控时间 / 已录时间 / 录制n分钟+停止录制 / 录制状态 /
#   是否监控 / 是否录制 / 录制内容 / 录制质量+设置清晰度 / 分段录制 /
#   转写文案 / 开播提醒 / 关播提醒 / 开录提醒 / 停录提醒 / 在线提醒 /
#   讲解商品记录 / 操作
# 前面 选择 / 序号 / 主播名称 是原软件的冻结列（有 nud_FrozenColumns 设置项）。
# 末尾 4 列是本版本额外补充的（原软件没有列，放在顶部详情条里），
# 默认隐藏，可在表头右键「列显示设置」里打开。
# (key, 标题, 默认宽, 对齐, 默认可见)
COLUMNS = [
    ("选择", "选择", 46, A.AlignCenter, True),
    ("序号", "序号", 46, A.AlignCenter, True),
    ("主播名称", "主播名称", 170, A.AlignLeft | A.AlignVCenter, True),
    ("直播状态", "直播状态", 82, A.AlignCenter, True),
    ("监控时间", "监控时间", 84, A.AlignCenter, True),
    ("已录时间", "已录时间", 84, A.AlignCenter, True),
    ("录制n分钟", "录制n分钟\n停止录制", 92, A.AlignCenter, True),
    ("录制状态", "录制状态", 78, A.AlignCenter, True),
    ("是否监控", "是否\n监控", 62, A.AlignCenter, True),
    ("是否录制", "是否\n录制", 62, A.AlignCenter, True),
    ("录制内容", "录制\n内容", 62, A.AlignCenter, True),
    ("清晰度", "录制质量\n设置清晰度", 96, A.AlignCenter, True),
    ("分段录制", "分段\n录制", 80, A.AlignCenter, True),
    ("转写文案", "转写\n文案", 62, A.AlignCenter, True),
    ("开播提醒", "开播\n提醒", 62, A.AlignCenter, True),
    ("关播提醒", "关播\n提醒", 62, A.AlignCenter, True),
    ("开录提醒", "开录\n提醒", 62, A.AlignCenter, True),
    ("停录提醒", "停录\n提醒", 62, A.AlignCenter, True),
    ("在线提醒", "在线\n提醒", 62, A.AlignCenter, True),
    ("讲解商品记录", "讲解商\n品记录", 82, A.AlignCenter, True),
    # ---- 本版本额外补充（原软件无此列，默认隐藏）----
    ("在线人数", "在线人数", 84, A.AlignCenter, False),
    ("粉丝量", "粉丝量", 88, A.AlignCenter, False),
    ("点赞量", "点赞量", 90, A.AlignCenter, False),
    ("直播标题", "直播标题", 240, A.AlignLeft | A.AlignVCenter, False),
]

COL_INDEX = {c[0]: i for i, c in enumerate(COLUMNS)}

# 每秒会变的列（只对这些列发 dataChanged，保留选中与滚动位置）
VOLATILE_KEYS = ("直播状态", "监控时间", "已录时间",
                 "录制状态", "是否监控", "是否录制", "在线人数")

# 颜色
C_LIVE_BG = QColor("#FFF4F4")
C_REC_BG = QColor("#FFE4E4")
C_ERR_FG = QColor("#C00000")
C_MUTED_FG = QColor("#9A9A9A")
C_GREEN = QColor("#0A7A0A")
C_ORANGE = QColor("#B06000")

REC_CONTENT_LABEL = {0: "视频", 1: "音频"}


def fmt_time_of_day(s):
    """把 "YYYY-MM-DD HH:MM:SS" 取成 "HH:MM:SS"（原软件「监控时间」列格式）。

    容错：已经是 HH:MM:SS 就原样返回；空值返回空串；绝不抛异常。
    """
    if not s:
        return ""
    s = str(s).strip()
    if not s:
        return ""
    if " " in s:
        s = s.split(" ", 1)[1]
    return s[:8] if len(s) >= 8 else s


def fmt_duration(sec):
    """把秒数格式化成 HH:MM:SS。

    容错：入参可能已经是 "HH:MM:SS" 字符串（例如从外部数据/历史文件载入），
    此时原样返回；非法值一律按 0 处理，绝不抛异常——
    表格模型的 data() 一旦抛异常会导致整个视图无法渲染。
    """
    if isinstance(sec, str):
        s = sec.strip()
        if not s:
            return "00:00:00"
        if ":" in s:
            return s          # 已是 HH:MM:SS，直接用
        try:
            sec = int(float(s))
        except (TypeError, ValueError):
            return "00:00:00"
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return "00:00:00"
    if sec < 0:
        sec = 0
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return "%02d:%02d:%02d" % (h, m, s)


def fmt_num(v):
    if v in (None, ""):
        return "-"
    try:
        n = int(v)
    except (TypeError, ValueError):
        return str(v)
    return "{:,}".format(n)


def to_int(v, default=0):
    """安全转 int。

    表格 data() 里任何未捕获异常都会让整个视图渲染失败，因此所有来自
    外部数据（历史文件 / 原软件数据 / 手工编辑）的数值都必须走这里。
    """
    if v is None or v == "":
        return default
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


# ============================================================ 表格模型

class AnchorTableModel(QAbstractTableModel):
    def __init__(self, anchors, recorder, parent=None):
        super().__init__(parent)
        self.anchors = anchors
        self.recorder = recorder
        self.visible = [c[0] for c in COLUMNS if c[4]]
        self._sync()

    # -------------------------------------------------- 可见列
    def _sync(self):
        self._cols = [COL_INDEX[k] for k in self.visible]

    def set_visible(self, keys):
        self.beginResetModel()
        # 保持 COLUMNS 的原始顺序
        order = [c[0] for c in COLUMNS]
        self.visible = sorted(set(keys), key=order.index)
        self._sync()
        self.endResetModel()

    def col_key(self, section):
        return self.visible[section]

    def is_volatile(self, section):
        return self.col_key(section) in VOLATILE_KEYS

    # -------------------------------------------------- 基本接口
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.anchors)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.visible)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[COL_INDEX[self.col_key(section)]][1]
        if role == Qt.ItemDataRole.ToolTipRole:
            return "右键可显示/隐藏列"
        return None

    def flags(self, index):
        base = super().flags(index)
        if index.isValid() and self.col_key(index.column()) == "选择":
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, sec = index.row(), index.column()
        if not (0 <= row < len(self.anchors)):
            return None
        a = self.anchors[row]
        key = self.col_key(sec)

        if role == Qt.ItemDataRole.CheckStateRole and key == "选择":
            return (Qt.CheckState.Checked if a.是否选择
                    else Qt.CheckState.Unchecked)

        if role == Qt.ItemDataRole.DisplayRole:
            return self._text(a, row, key)
        if role == Qt.ItemDataRole.BackgroundRole:
            if a.是否正在录制:
                return QBrush(C_REC_BG)
            if a.是否开播:
                return QBrush(C_LIVE_BG)
            return None
        if role == Qt.ItemDataRole.ForegroundRole:
            if a.错误:
                return QBrush(C_ERR_FG)
            if key in ("是否监控", "是否录制") and not self._flag_text(a, key):
                return QBrush(C_MUTED_FG)
            return None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(COLUMNS[COL_INDEX[key]][3])
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(a)
        if role == Qt.ItemDataRole.FontRole and a.是否正在录制:
            f = QFont()
            f.setBold(True)
            return f
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if (index.isValid() and role == Qt.ItemDataRole.CheckStateRole
                and self.col_key(index.column()) == "选择"):
            a = self.anchors[index.row()]
            a.是否选择 = (value == Qt.CheckState.Checked.value
                        or value == Qt.CheckState.Checked)
            self.dataChanged.emit(index, index, [role])
            return True
        return False

    # -------------------------------------------------- 单元格文本
    @staticmethod
    def _flag_text(a, key):
        """布尔列一律显示 是 / 否（与原软件一致）。

        原软件里 是否监控 / 是否录制 / 各类提醒 都是「是」「否」两个字符串，
        不是勾叉符号。
        """
        m = {
            "是否监控": a.是否监控,
            "是否录制": a.是否录制,
            "开播提醒": a.是否开播提醒,
            "关播提醒": a.是否关播提醒,
            "开录提醒": a.是否开始录制提醒,
            "停录提醒": a.是否停止录制提醒,
            "在线提醒": a.是否在线人数提醒,
            "讲解商品记录": a.是否讲解商品记录保存,
        }
        if key in m:
            return "是" if m[key] else "否"
        return ""

    def _text(self, a, row, key):
        if key == "选择":
            return ""
        if key == "序号":
            return str(row + 1)
        if key == "主播名称":
            return a.主播名称 or "-"
        if key == "直播状态":
            # 取值与原软件一致：未监控 / 请开启监控 / 已开播 / 等待开播 / 无直播权限
            if not a.是否监控:
                return "请开启监控" if a.是否录制 else "未监控"
            if a.错误:
                return "无直播权限"
            return "已开播" if a.是否开播 else "等待开播"
        if key == "在线人数":
            if a.显示在线人数:
                return a.显示在线人数
            return fmt_num(a.在线人数) if a.是否开播 else "-"
        if key == "粉丝量":
            return fmt_num(a.粉丝量)
        if key == "点赞量":
            return fmt_num(a.点赞量)
        if key == "监控时间":
            # 原软件「监控时间」= 最后一次刷新该直播间的时间（最后监控时间），
            # 不是已监控时长。格式 HH:mm:ss。
            return fmt_time_of_day(a.最后监控时间)
        if key == "已录时间":
            if a.是否正在录制:
                return fmt_duration(self.recorder.elapsed(a.key))
            return fmt_duration(a.已录时间)
        if key == "录制n分钟":
            n = to_int(a.录制多少分钟停止, 0)
            return "%d 分钟" % n if n else "不限"
        if key == "录制状态":
            if a.是否正在录制:
                return "录制中"
            return a.录制状态 or "未录制"
        if key in ("是否监控", "是否录制", "开播提醒", "关播提醒",
                   "开录提醒", "停录提醒", "在线提醒", "讲解商品记录"):
            return self._flag_text(a, key)
        if key == "录制内容":
            # RecContent 枚举：0=Video(视频) 1=Audio(音频)
            return REC_CONTENT_LABEL.get(to_int(a.录制内容, 0), "视频")
        if key == "清晰度":
            # 原软件是「档位->启用」字典 + 优先顺序，按优先级显示启用档位
            lst = enabled_ratios(a)
            return ">".join(lst) if lst else "未启用"
        if key == "分段录制":
            if a.是否分段录制:
                return "%d 分钟/段" % to_int(a.分段录制几分钟, 0)
            return "否"
        if key == "转写文案":
            return "********" if a.是否转写话术文案 else ""
        if key == "直播标题":
            return a.直播标题 or ""
        return ""

    @staticmethod
    def _tooltip(a):
        lines = ["主播：%s" % (a.主播名称 or "-"),
                 "抖音号：%s" % (a.抖音号 or "-"),
                 "唯一码：%s" % (a.主播唯一码 or "-"),
                 "房间号：%s" % (a.房间号 or "-"),
                 "直播状态：%s" % ("已开播" if a.是否开播 else "等待开播"),
                 "最后监控时间：%s" % (a.最后监控时间 or "-"),
                 "清晰度优先顺序：%s" % (">".join(enabled_ratios(a)) or "-")]
        if a.录制状态:
            lines.append("录制状态：%s" % a.录制状态)
        if a.是否分段录制:
            lines.append("分段录制：每 %d 分钟" % int(a.分段录制几分钟 or 0))
        if a.录制多少分钟停止:
            lines.append("录满停止：%d 分钟" % int(a.录制多少分钟停止))
        if a.错误:
            lines.append("错误：%s" % a.错误)
        if a.直播地址:
            lines.append("地址：%s" % a.直播地址)
        if a.备注:
            lines.append("备注：%s" % a.备注)
        return "\n".join(lines)

    # -------------------------------------------------- 刷新
    def refresh_all(self):
        self.beginResetModel()
        self.endResetModel()

    def refresh_volatile(self):
        """只刷新每秒会变的列，保留选中与滚动位置。"""
        if not self.anchors or not self.visible:
            return
        idxs = [i for i, k in enumerate(self.visible) if k in VOLATILE_KEYS]
        if not idxs:
            return
        roles = [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.BackgroundRole,
                 Qt.ItemDataRole.ForegroundRole]
        for c in idxs:
            self.dataChanged.emit(self.index(0, c),
                                  self.index(len(self.anchors) - 1, c), roles)


# ============================================================ 跨线程桥

class Bridge(QObject):
    """监控线程 -> GUI 线程 的信号桥。"""
    logged = Signal(str, str)          # level, text
    stream_ready = Signal(str, str)    # anchor_key, stream_url


# ============================================================ 主窗口

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(config.APP_TITLE)
        self.resize(1500, 840)
        self.setMinimumSize(1080, 640)

        self.anchors = store.load_anchors()
        self.cfg = config.load_config()

        self.bridge = Bridge(self)
        self.bridge.logged.connect(self._on_log,
                                   Qt.ConnectionType.QueuedConnection)
        self.bridge.stream_ready.connect(self._on_stream_ready,
                                         Qt.ConnectionType.QueuedConnection)

        self.monitor = Monitor(self._anchors_ref, lambda: self.cfg,
                               emit=self.bridge.logged.emit)
        self._client = self.monitor.client
        self._busy = False
        self._pending_stream = None

        self._build_ui()
        self._build_menus()
        self._build_toolbar()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

        self._log("程序启动，共加载 %d 个主播" % len(self.anchors), "ok")
        if not config.FFMPEG:
            self._log("未找到 ffmpeg，录制/预览/转换功能不可用"
                      "（请把 ffmpeg 文件夹放到程序目录）", "warn")
        if not config.FFPLAY:
            self._log("未找到 ffplay，直播流预览不可用", "warn")
        if self.cfg.get("启动时自动开启监控"):
            self._log("按设置自动开启监控…")
            QTimer.singleShot(600, self.on_start_monitor)

    # -------------------------------------------------- 数据引用
    def _anchors_ref(self):
        return self.anchors

    def _checked(self):
        """「拉篮」勾选的主播（对应原软件 列表拉篮选择_*）。"""
        return [a for a in self.anchors if a.是否选择]

    # -------------------------------------------------- 界面
    def _build_ui(self):
        splitter = QSplitter(Qt.Orientation.Vertical, self)

        # ---- 顶部详情条（对应原软件 pl_LiveTop）
        self.top_bar = QLabel("请选择主播", self)
        self.top_bar.setStyleSheet(
            "background:#FFFFFF;border:1px solid #E0E0E0;"
            "padding:3px 8px;color:#333;")
        self.top_bar.setMinimumHeight(24)

        # ---- 表格
        self.model = AnchorTableModel(self.anchors, self.monitor.recorder, self)
        self.table = QTableView(splitter)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.doubleClicked.connect(self._on_double_clicked)
        self.table.clicked.connect(self._on_clicked)

        hh = self.table.horizontalHeader()
        for i, key in enumerate(self.model.visible):
            self.table.setColumnWidth(i, COLUMNS[COL_INDEX[key]][2])
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._on_header_menu)

        # ---- 日志
        logbox = QWidget(splitter)
        lay = QVBoxLayout(logbox)
        lay.setContentsMargins(8, 2, 8, 6)
        lay.setSpacing(2)
        lbl = QLabel("运行日志")
        lbl.setStyleSheet("color:#555;font-weight:bold;")
        lay.addWidget(lbl)
        self.log = QPlainTextEdit(logbox)
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setStyleSheet("background:#FAFAFA;border:1px solid #E0E0E0;")
        lay.addWidget(self.log)

        splitter.addWidget(self.table)
        splitter.addWidget(logbox)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([560, 200])

        holder = QWidget(self)
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(0)
        hv.addWidget(self.top_bar)
        hv.addWidget(splitter)
        self.setCentralWidget(holder)

        # ---- 状态栏
        self.status_label = QLabel("就绪")
        sb = QStatusBar(self)
        sb.addWidget(self.status_label, 1)
        self.setStatusBar(sb)

    def _act(self, text, slot, shortcut=None, tip=None):
        a = QAction(text, self)
        a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setStatusTip(tip or text)
        return a

    def _build_toolbar(self):
        tb = QToolBar("主工具栏", self)
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(tb)

        self.act_add = self._act("＋ 添加主播", self.on_add, "Ctrl+N")
        self.act_batch = self._act("批量导入", self.on_batch_import, "Ctrl+I")
        self.act_mon = self._act("▶ 开启监控录制", self.on_toggle_monitor, "F5")
        self.act_rec = self._act("⏹ 一键停止录制", self.on_stop_all_record, "F9")
        tb.addAction(self.act_add)
        tb.addAction(self.act_batch)
        tb.addSeparator()
        tb.addAction(self.act_mon)
        tb.addAction(self.act_rec)
        tb.addAction(self._act("⟳ 立即检测开播", self.on_check_now, "F6"))
        tb.addSeparator()
        tb.addAction(self._act("热门直播", self.on_hot, "F7"))
        tb.addAction(self._act("数据统计", self.on_stats, "F8"))
        tb.addAction(self._act("导出 Excel", self.on_export, "Ctrl+E"))
        tb.addSeparator()
        tb.addAction(self._act("录制设置", self.on_record_settings))
        tb.addAction(self._act("视频转码", self.on_convert))
        tb.addAction(self._act("设置", self.on_settings, "Ctrl+,"))
        tb.addAction(self._act("使用教程", self.on_tutorial, "F1"))

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        tb.addWidget(spacer)
        tb.addAction(self._act("打开数据目录", self.on_open_data_dir))
        tb.addAction(self._act("打开录制目录", self.on_open_video_dir))

    def _build_menus(self):
        mb = self.menuBar()

        m = mb.addMenu("文件(&F)")
        m.addAction(self._act("添加主播…", self.on_add, "Ctrl+N"))
        m.addAction(self._act("批量导入主播…", self.on_batch_import, "Ctrl+I"))
        m.addSeparator()
        m.addAction(self._act("导出主播数据（Excel）…", self.on_export, "Ctrl+E"))
        m.addAction(self._act("导出采集记录（Excel）…", self.on_export_records))
        m.addAction(self._act("导出列表勾选数据 TXT…",
                              self.on_export_checked_txt))
        m.addSeparator()
        m.addAction(self._act("打开数据目录", self.on_open_data_dir))
        m.addAction(self._act("打开录制目录", self.on_open_video_dir))
        m.addSeparator()
        m.addAction(self._act("退出", self.close, "Ctrl+Q"))

        m = mb.addMenu("主播(&A)")
        m.addAction(self._act("加入监控", lambda: self._set_flag("是否监控", True)))
        m.addAction(self._act("退出监控", lambda: self._set_flag("是否监控", False)))
        m.addSeparator()
        m.addAction(self._act("加入录制", lambda: self._set_flag("是否录制", True)))
        m.addAction(self._act("退出录制", lambda: self._set_flag("是否录制", False)))
        m.addAction(self._act("一键停止全部录制", self.on_stop_all_record, "F9"))
        m.addSeparator()
        m.addAction(self._act("独立设置功能…", self.on_anchor_settings))
        m.addAction(self._act("分段录制每段几分钟设置…",
                              self.on_split_rec_setting))
        m.addAction(self._act("已录时间归零", self.on_reset_elapsed))
        m.addSeparator()
        m.addAction(self._act("预览直播流", self.on_preview_stream))
        m.addAction(self._act("在浏览器打开直播间", self.on_open_in_browser))
        m.addSeparator()
        m.addAction(self._act("删除当前选择", self.on_delete, "Del"))

        m = mb.addMenu("工具(&T)")
        m.addAction(self._act("热门直播…", self.on_hot, "F7"))
        m.addAction(self._act("直播数据统计…", self.on_stats, "F8"))
        m.addSeparator()
        m.addAction(self._act("录制设置…", self.on_record_settings))
        m.addAction(self._act("视频转码 / 压缩…", self.on_convert))
        m.addAction(self._act("推送消息设置…", self.on_push_settings))
        m.addSeparator()
        m.addAction(self._act("浏览器设置…", self.on_browser_settings))
        m.addAction(self._act("运行日志…", self.on_show_log))

        m = mb.addMenu("设置(&S)")
        m.addAction(self._act("通用设置…", self.on_settings, "Ctrl+,"))
        m.addAction(self._act("录制设置…", self.on_record_settings))
        m.addAction(self._act("浏览器设置…", self.on_browser_settings))
        m.addSeparator()
        m.addAction(self._act("列显示 / 隐藏…", self.on_column_chooser))

        m = mb.addMenu("帮助(&H)")
        m.addAction(self._act("使用教程", self.on_tutorial, "F1"))
        m.addAction(self._act("关于", self.on_about))

    # -------------------------------------------------- 定时刷新
    def _tick(self):
        self.model.refresh_volatile()
        self._update_status()
        self._update_top_bar()

    def _update_status(self):
        n_mon = sum(1 for a in self.anchors if a.是否监控)
        n_rec = self.monitor.recorder.active_count()
        self.status_label.setText(
            "监控：%s　|　主播 %d 个（监控 %d）　|　录制中 %d　|　轮次 %d　|　"
            "上次检测 %s　|　ffmpeg：%s"
            % ("运行中" if self.monitor.running else "已停止",
               len(self.anchors), n_mon, n_rec, self.monitor.cycles,
               self.monitor.last_run or "-",
               "已就绪" if config.FFMPEG else "未找到"))

    def _update_top_bar(self):
        items = self._selected()
        if not items:
            self.top_bar.setText(
                "共 %d 个主播　|　监控 %d　|　录制中 %d　|　勾选 %d"
                % (len(self.anchors),
                   sum(1 for a in self.anchors if a.是否监控),
                   self.monitor.recorder.active_count(), len(self._checked())))
            return
        a = items[0]
        self.top_bar.setText(
            "【%s】　抖音号 %s　|　直播状态 %s　|　在线 %s　|　粉丝 %s　|　"
            "监控时间 %s　|　已录时间 %s　|　录制状态 %s　|　%s"
            % (a.主播名称 or "-", a.抖音号 or "-",
               "已开播" if a.是否开播 else "等待开播",
               a.显示在线人数 or fmt_num(a.在线人数), fmt_num(a.粉丝量),
               fmt_time_of_day(a.最后监控时间) or "-",
               fmt_duration(self.monitor.recorder.elapsed(a.key)
                            if a.是否正在录制 else a.已录时间),
               a.录制状态 or "未录制", a.直播标题 or ""))

    # -------------------------------------------------- 日志
    def _on_log(self, level, text):
        color = {"ok": "#0A7A0A", "warn": "#B06000",
                 "error": "#C00000", "info": "#0A5AA0"}.get(level, "#777777")
        ts = time.strftime("%H:%M:%S")
        safe = (str(text).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
        self.log.appendHtml(
            '<span style="color:#9A9A9A">[%s]</span> '
            '<span style="color:%s">%s</span>' % (ts, color, safe))
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _log(self, text, level="debug"):
        self._on_log(level, text)

    # -------------------------------------------------- 选中项
    def _selected(self):
        rows = sorted({i.row() for i in
                       self.table.selectionModel().selectedRows()})
        return [self.anchors[r] for r in rows if 0 <= r < len(self.anchors)]

    def _targets(self, batch=False):
        """取操作对象：批量操作取勾选行，否则取选中行。"""
        if batch:
            got = self._checked()
            if not got:
                QMessageBox.information(
                    self, "提示", "请先在「选择」列勾选要批量操作的主播")
            return got
        got = self._selected()
        if not got:
            QMessageBox.information(self, "提示", "请先选择主播")
        return got

    # -------------------------------------------------- 表格交互
    def _on_clicked(self, index):
        if self.model.col_key(index.column()) == "选择":
            a = self.anchors[index.row()]
            a.是否选择 = not a.是否选择
            self.model.dataChanged.emit(
                index, index, [Qt.ItemDataRole.CheckStateRole])
            self._update_top_bar()

    def _on_double_clicked(self, index):
        key = self.model.col_key(index.column())
        if key == "是否监控":
            self._toggle_flag("是否监控")
        elif key == "是否录制":
            self._toggle_flag("是否录制")
        elif key == "选择":
            return
        else:
            self.on_anchor_settings()

    def _on_header_menu(self, pos):
        """表头右键：列显示 / 隐藏（对应原软件 MS_DatagridviewHide）。"""
        menu = QMenu(self)
        for key, title, _w, _a, _v in COLUMNS:
            if key in ("选择", "序号"):
                continue
            act = QAction(title, menu)
            act.setCheckable(True)
            act.setChecked(key in self.model.visible)
            act.triggered.connect(
                lambda _c=False, k=key: self._toggle_column(k))
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction(self._act("全部显示", self.on_show_all_columns))
        menu.addAction(self._act("恢复默认", self.on_reset_columns))
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _toggle_column(self, key):
        vis = list(self.model.visible)
        if key in vis:
            if len(vis) <= 2:
                return
            vis.remove(key)
        else:
            vis.append(key)
        self._apply_columns(vis)

    def _apply_columns(self, vis):
        self.model.set_visible(vis)
        for i, key in enumerate(self.model.visible):
            self.table.setColumnWidth(i, COLUMNS[COL_INDEX[key]][2])
        self._update_top_bar()

    def on_show_all_columns(self):
        self._apply_columns([c[0] for c in COLUMNS])

    def on_reset_columns(self):
        self._apply_columns([c[0] for c in COLUMNS if c[4]])

    def on_column_chooser(self):
        from PySide6.QtWidgets import QDialogButtonBox, QCheckBox
        dlg = QDialog(self)
        dlg.setWindowTitle("列显示 / 隐藏")
        dlg.resize(300, 460)
        lay = QVBoxLayout(dlg)
        boxes = []
        for key, title, _w, _a, _v in COLUMNS:
            if key in ("选择", "序号"):
                continue
            cb = QCheckBox(title, dlg)
            cb.setChecked(key in self.model.visible)
            lay.addWidget(cb)
            boxes.append((key, cb))
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, dlg)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_columns([k for k, cb in boxes if cb.isChecked()])

    # -------------------------------------------------- 右键菜单（原版全项）
    def _on_context_menu(self, pos):
        idx = self.table.indexAt(pos)
        if idx.isValid():
            self.table.selectRow(idx.row())
        has = bool(self._selected())
        menu = QMenu(self)
        ad = menu.addAction

        ad(self._act("当前主播_立即检测开播", self.on_check_selected))
        ad(self._act("当前主播_播放直播源", self.on_preview_stream))
        menu.addSeparator()
        ad(self._act("当前主播_加入监控",
                     lambda: self._set_flag("是否监控", True)))
        ad(self._act("当前主播_退出监控",
                     lambda: self._set_flag("是否监控", False)))
        ad(self._act("当前主播_加入录制",
                     lambda: self._set_flag("是否录制", True)))
        ad(self._act("当前主播_退出录制",
                     lambda: self._set_flag("是否录制", False)))
        # 修改录制多少分钟停止录制快捷值（子菜单）
        sub = menu.addMenu("当前主播_修改录制多少分钟停止录制快捷值")
        for mins in (0, 5, 10, 30, 60, 120, 180):
            label = "不限制" if mins == 0 else "%d 分钟" % mins
            sub.addAction(self._act(
                label, lambda _c=False, m=mins: self._set_stop_minutes(m)))
        sub.addSeparator()
        sub.addAction(self._act("自定义…",
                                lambda: self._set_stop_minutes(None)))
        ad(self._act("当前主播_已录时间归零重新录制",
                     self.on_reset_elapsed_restart))
        menu.addSeparator()
        ad(self._act("当前主播_独立设置功能", self.on_anchor_settings))
        menu.addSeparator()
        ad(self._act("当前主播_移动至序号", self.on_move_to_index))
        ad(self._act("当前主播_修改备注信息", self.on_edit_remark))
        ad(self._act("当前主播_复制直播源", self.on_copy_stream))
        ad(self._act("当前主播_复制直播网址", self.on_copy_url))
        ad(self._act("当前主播_复制主播整体数据", self.on_copy_data))
        ad(self._act("当前主播_删除当前选择", self.on_delete))
        menu.addSeparator()
        ad(self._act("当前主播_打开当前视频保存目录", self.on_open_anchor_video))
        ad(self._act("当前主播_打开主播信息目录并选择",
                     self.on_open_anchor_info))
        ad(self._act("当前主播_打开记录数据文件夹",
                     lambda: self._open_dir(config.RECORD_DATA_DIR)))
        menu.addSeparator()
        ad(self._act("全部主播_复制直播网址", self.on_copy_all_url))
        ad(self._act("列表拉篮选择_勾选", lambda: self.on_lan_check(True)))
        ad(self._act("列表拉篮选择_不选", lambda: self.on_lan_check(False)))
        ad(self._act("列表拉篮选择_反选", self.on_lan_reverse))
        menu.addSeparator()
        ad(self._act("当前主播_运行日志", self.on_show_log))
        for a in menu.actions():
            a.setEnabled(has or "全部主播" in a.text() or "列表拉篮" in a.text())
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _on_batch_menu(self, pos):
        """「批量修改」按钮菜单（原软件 MS_BatchModify）。"""
        menu = QMenu(self)
        ad = menu.addAction
        ad(self._act("列表勾选_加入监控",
                     lambda: self._set_flag("是否监控", True, True)))
        ad(self._act("列表勾选_退出监控",
                     lambda: self._set_flag("是否监控", False, True)))
        ad(self._act("列表勾选_加入录制",
                     lambda: self._set_flag("是否录制", True, True)))
        ad(self._act("列表勾选_退出录制",
                     lambda: self._set_flag("是否录制", False, True)))
        menu.addSeparator()
        ad(self._act("列表勾选_修改录制多少分钟停止录制",
                     lambda: self._set_stop_minutes(None, True)))
        ad(self._act("列表勾选_修改录制多少分钟停止录制快捷值",
                     self.on_batch_stop_quick))
        ad(self._act("列表勾选_分段录制每段几分钟设置",
                     lambda: self.on_split_rec_setting(True)))
        ad(self._act("列表勾选_录制清晰度与优先顺序",
                     lambda: self.on_quality_setting(True)))
        ad(self._act("列表勾选_录制视频(有视频有声)",
                     lambda: self.on_set_content(0, True)))
        ad(self._act("列表勾选_录制音频(无视频有声)",
                     lambda: self.on_set_content(2, True)))
        menu.addSeparator()
        ad(self._act("列表勾选_已录时间归零",
                     lambda: self.on_reset_elapsed(True)))
        ad(self._act("列表勾选_已录时间归零重新录制",
                     lambda: self.on_reset_elapsed_restart(True)))
        ad(self._act("列表勾选_修改备注", lambda: self.on_edit_remark(True)))
        menu.addSeparator()
        ad(self._act("列表勾选_提醒与记录设置",
                     lambda: self.on_remind_setting(True)))
        for name in ("开播提醒", "关播提醒", "开始录制提醒",
                     "停止录制提醒", "在线人数提醒"):
            ad(self._act("列表勾选_%s" % name,
                         lambda _c=False, n=name: self.on_toggle_remind(n, True)))
        ad(self._act("列表勾选_登录状态访问直播间",
                     lambda: self.on_toggle_field(
                         "是否登录状态访问直播间", True)))
        ad(self._act("列表勾选_语音转写文案",
                     lambda: self.on_toggle_field("是否转写话术文案", True)))
        ad(self._act("列表勾选_讲解商品记录",
                     lambda: self.on_toggle_field("是否讲解商品记录保存", True)))
        ad(self._act("列表勾选_购物车商品记录",
                     lambda: self.on_toggle_field(
                         "是否购物车商品记录保存", True)))
        menu.addSeparator()
        ad(self._act("列表勾选_删除主播(不删除录制数据)",
                     lambda: self.on_delete(True)))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # -------------------------------------------------- 勾选 / 拉篮
    def on_lan_check(self, value):
        for a in self.anchors:
            a.是否选择 = value
        self.model.refresh_all()
        self._update_top_bar()

    def on_lan_reverse(self):
        for a in self.anchors:
            a.是否选择 = not a.是否选择
        self.model.refresh_all()
        self._update_top_bar()

    # -------------------------------------------------- 标志位
    def _toggle_flag(self, name):
        items = self._selected()
        if not items:
            return
        new = not all(getattr(a, name) for a in items)
        self._set_flag(name, new)

    def _set_flag(self, name, value, batch=False):
        items = self._targets(batch)
        if not items:
            return
        for a in items:
            setattr(a, name, value)
            if name == "是否监控":
                if not value:
                    a.是否正在监控 = False
                    a.最后监控时间 = ""
            if name == "是否录制" and not value \
                    and self.monitor.recorder.is_recording(a.key):
                self.monitor.recorder.stop(a.key)
                a.是否正在录制 = False
                a.录制状态 = "已停止"
                a.已录时间 = self.monitor.recorder.elapsed(a.key)
        self.model.refresh_volatile()
        self._save()
        self._log("%s %d 个主播：%s" % (
            "批量设置" if batch else "设置", len(items),
            ("加入" if value else "退出") + name.replace("是否", "")))

    def _set_stop_minutes(self, minutes, batch=False):
        items = self._targets(batch)
        if not items:
            return
        if minutes is None:
            minutes, ok = QInputDialog.getInt(
                self, "修改录制多少分钟停止录制", "录制多少分钟后自动停止（0=不限制）：",
                int(items[0].录制多少分钟停止 or 0), 0, 1440, 5)
            if not ok:
                return
        for a in items:
            a.录制多少分钟停止 = int(minutes)
        self.model.refresh_volatile()
        self._save()
        self._log("已设置 %d 个主播：录满 %s 停止录制"
                  % (len(items), "%d 分钟" % minutes if minutes else "不限制"))

    def on_batch_stop_quick(self):
        menu = QMenu(self)
        for mins in (0, 5, 10, 30, 60, 120, 180):
            label = "不限制" if mins == 0 else "%d 分钟" % mins
            menu.addAction(self._act(
                label, lambda _c=False, m=mins: self._set_stop_minutes(m, True)))
        menu.exec(self.table.viewport().mapToGlobal(
            self.table.viewport().rect().center()))

    def on_set_content(self, content, batch=False):
        items = self._targets(batch)
        if not items:
            return
        for a in items:
            a.录制内容 = content
        self.model.refresh_volatile()
        self._save()
        self._log("已设置 %d 个主播录制内容为 %s"
                  % (len(items), REC_CONTENT_LABEL[content]))

    def on_toggle_remind(self, name, batch=False):
        items = self._targets(batch)
        if not items:
            return
        new = not all(getattr(a, name) for a in items)
        for a in items:
            setattr(a, name, new)
        self.model.refresh_volatile()
        self._save()

    def on_toggle_field(self, name, batch=False):
        items = self._targets(batch)
        if not items:
            return
        new = not all(getattr(a, name) for a in items)
        for a in items:
            setattr(a, name, new)
        self.model.refresh_volatile()
        self._save()

    def on_remind_setting(self, batch=False):
        items = self._targets(batch)
        if not items:
            return
        from PySide6.QtWidgets import QDialogButtonBox, QCheckBox, QGridLayout
        fields = [("是否开播提醒", "开播提醒"), ("是否关播提醒", "关播提醒"),
                  ("是否开始录制提醒", "开始录制提醒"),
                  ("是否停止录制提醒", "停止录制提醒"),
                  ("是否在线人数提醒", "在线人数提醒"),
                  ("是否讲解商品记录保存", "讲解商品记录"),
                  ("是否购物车商品记录保存", "购物车商品记录"),
                  ("是否登录状态访问直播间", "登录状态访问直播间"),
                  ("是否转写话术文案", "语音转写文案")]
        dlg = QDialog(self)
        dlg.setWindowTitle("提醒与记录设置（%d 个主播）" % len(items))
        g = QGridLayout(dlg)
        boxes = []
        for i, (attr, label) in enumerate(fields):
            cb = QCheckBox(label, dlg)
            cb.setChecked(bool(getattr(items[0], attr)))
            g.addWidget(cb, i // 2, i % 2)
            boxes.append((attr, cb))
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, dlg)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        g.addWidget(bb, len(fields) // 2 + 1, 0, 1, 2)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            for attr, cb in boxes:
                for a in items:
                    setattr(a, attr, cb.isChecked())
            self.model.refresh_volatile()
            self._save()

    # -------------------------------------------------- 动作
    def _autostart_monitor(self):
        """新增主播后，若其中有人处于「监控」状态且监控未运行，自动开启。

        对应原软件行为：添加/导入主播后无需再手动点一次「开启监控」，
        避免「加进来了却一直没检测、录不到」的困惑。
        """
        if self.monitor.running:
            return
        if not any(a.是否监控 for a in self.anchors):
            return
        self.monitor.start()
        self.act_mon.setText("■ 停止监控录制")
        self._log("检测到监控主播，已自动开启监控（无需手动检测）", "ok")

    def on_add(self):
        dlg = AddAnchorDialog(self, self._client, self.cfg)
        a = dlg.exec_get_anchor()
        if not a:
            return
        if a.主播唯一码 and any(x.主播唯一码 == a.主播唯一码
                              for x in self.anchors):
            QMessageBox.information(self, "提示", "该主播已在列表中")
            return
        self.anchors.append(a)
        self.model.refresh_all()
        self._save()
        self._log("已添加主播：%s（%s）" % (a.主播名称, a.抖音号 or a.主播唯一码),
                  "ok")
        self._autostart_monitor()

    def on_batch_import(self):
        dlg = BatchImportDialog(self, self._client, self.cfg)
        items = dlg.exec_get_anchors()
        if not items:
            return
        exist = {a.主播唯一码 for a in self.anchors if a.主播唯一码}
        added = 0
        for a in items:
            if a.主播唯一码 in exist:
                continue
            self.anchors.append(a)
            exist.add(a.主播唯一码)
            added += 1
        self.model.refresh_all()
        self._save()
        self._log("批量导入：新增 %d 个主播（跳过重复 %d 个）"
                  % (added, len(items) - added), "ok")
        QMessageBox.information(self, "导入完成",
                                "新增 %d 个主播，跳过重复 %d 个。"
                                % (added, len(items) - added))
        self._autostart_monitor()

    def on_delete(self, batch=False):
        items = self._targets(batch)
        if not items:
            return
        if QMessageBox.question(
                self, "确认", "确定删除选中的 %d 个主播？\n（录制文件不会被删除）"
                % len(items),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        for a in items:
            self.monitor.recorder.stop(a.key)
            if a in self.anchors:
                self.anchors.remove(a)
        self.model.refresh_all()
        self._save()
        self._log("已删除 %d 个主播" % len(items), "warn")

    def on_move_to_index(self):
        items = self._selected()
        if not items:
            return
        a = items[0]
        cur = self.anchors.index(a) + 1
        n, ok = QInputDialog.getInt(
            self, "移动至序号", "把「%s」移动到第几行？" % a.主播名称,
            cur, 1, len(self.anchors))
        if not ok:
            return
        self.anchors.remove(a)
        self.anchors.insert(n - 1, a)
        self.model.refresh_all()
        self._save()
        self._log("「%s」已移动到第 %d 行" % (a.主播名称, n), "ok")

    def on_edit_remark(self, batch=False):
        items = self._targets(batch)
        if not items:
            return
        cur = items[0].备注 or ""
        text, ok = QInputDialog.getText(
            self, "修改备注信息",
            "为 %d 个主播设置备注：" % len(items) if batch
            else "「%s」的备注：" % items[0].主播名称, text=cur)
        if not ok:
            return
        for a in items:
            a.备注 = text
        self.model.refresh_volatile()
        self._save()
        self._log("已修改 %d 个主播的备注" % len(items), "ok")

    def on_start_monitor(self):
        if self.monitor.running:
            return
        if not any(a.是否监控 for a in self.anchors):
            QMessageBox.information(
                self, "提示",
                "当前没有主播加入监控。\n请先勾选主播的「是否监控」列，"
                "或右键 → 当前主播_加入监控。")
            return
        self.monitor.start()
        self.act_mon.setText("■ 停止监控录制")
        self._log("监控已启动", "ok")

    def on_toggle_monitor(self):
        if self.monitor.running:
            self.monitor.stop()
            self.act_mon.setText("▶ 开启监控录制")
            for a in self.anchors:
                a.是否正在监控 = False
            self.model.refresh_volatile()
            self._log("监控已停止", "warn")
            return
        self.on_start_monitor()

    # ---- 一键录制开关（原软件没有独立的按钮，这里按需求补上）
    def on_stop_all_record(self):
        n = self.monitor.recorder.active_count()
        if not n:
            QMessageBox.information(self, "提示", "当前没有正在录制的任务")
            return
        if QMessageBox.question(
                self, "确认", "停止全部 %d 个录制任务？" % n,
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        for a in self.anchors:
            if self.monitor.recorder.is_recording(a.key):
                a.已录时间 = self.monitor.recorder.elapsed(a.key)
        self.monitor.recorder.stop_all()
        for a in self.anchors:
            a.是否正在录制 = False
            a.录制状态 = "已停止"
        self.model.refresh_volatile()
        self._log("已停止全部录制（%d 个）" % n, "warn")

    def on_check_selected(self):
        items = self._selected()
        if not items:
            return
        self._check_async(items)

    def on_check_now(self):
        if self._busy:
            return
        items = [a for a in self.anchors if a.是否监控] or list(self.anchors)
        if not items:
            QMessageBox.information(self, "提示", "请先添加主播")
            return
        self._check_async(items)

    def _check_async(self, items):
        self._busy = True
        self._log("开始检测 %d 个主播…" % len(items))

        import threading

        def work():
            try:
                for a in items:
                    try:
                        self.monitor._check_one(a)
                    except Exception as e:  # noqa: BLE001
                        self.bridge.logged.emit(
                            "error", "「%s」检测异常：%s" % (a.主播名称, e))
                    self.monitor.client.sleep()
                self.bridge.logged.emit("ok", "检测完成")
            finally:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()

    def on_hot(self):
        dlg = HotLiveDialog(self, self._client, self.anchors, self.cfg)
        dlg.exec()
        if dlg.added:
            self.model.refresh_all()
            self._save()
            self._log("从热门直播加入 %d 个主播" % dlg.added, "ok")
            self._autostart_monitor()

    def on_stats(self):
        self._stats_win = StatsWindow(self, self.anchors)
        self._stats_win.show()

    def on_convert(self):
        dlg = ConvertDialog(self, self.cfg)
        dlg.exec()

    def on_record_settings(self):
        dlg = RecordSettingsDialog(self, self.cfg)
        cfg = dlg.exec_get_config()
        if cfg:
            self.cfg = cfg
            config.save_config(cfg)
            self._log("录制设置已保存", "ok")

    def on_browser_settings(self):
        dlg = BrowserSettingsDialog(self, self.cfg)
        cfg = dlg.exec_get_config()
        if cfg:
            self.cfg = cfg
            config.save_config(cfg)
            self._log("浏览器设置已保存", "ok")

    def on_push_settings(self):
        dlg = PushSettingsDialog(self, self.cfg)
        cfg = dlg.exec_get_config()
        if cfg:
            self.cfg = cfg
            config.save_config(cfg)
            self._log("推送设置已保存", "ok")

    def on_tutorial(self):
        self._tutorial = TutorialWindow(self)
        self._tutorial.show()

    def on_about(self):
        QMessageBox.information(
            self, "关于",
            "%s\n\n"
            "原软件：DY直播数据分析（.NET / WinForms）\n"
            "本版本：按原软件功能与界面结构等价重写（Python + PySide6）\n"
            "运行环境：Python 3.8+ / PySide6\n\n"
            "功能：主播监控 / 自动录制 / 分段录制 / 开播关播提醒 /\n"
            "      数据采集与导出 / 热门直播 / 视频转码 / 群机器人推送\n\n"
            "数据目录：%s" % (config.APP_TITLE, config.DATA_DIR))

    # ---- 导出
    def on_export(self):
        if not self.anchors:
            QMessageBox.information(self, "提示", "列表为空")
            return
        default = os.path.join(
            config.DATA_DIR,
            "主播数据_%s.xlsx" % time.strftime("%Y%m%d_%H%M%S"))
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Excel", default, "Excel 工作簿 (*.xlsx)")
        if not path:
            return
        rows = []
        for i, a in enumerate(self.anchors, start=1):
            rows.append([i, a.主播名称,
                         "已开播" if a.是否开播 else "等待开播",
                         a.显示在线人数 or a.在线人数, a.粉丝量, a.点赞量,
                         fmt_time_of_day(a.最后监控时间),
                         fmt_duration(a.已录时间),
                         a.录制状态 or "未录制",
                         "是" if a.是否监控 else "否",
                         "是" if a.是否录制 else "否",
                         REC_CONTENT_LABEL.get(int(a.录制内容 or 0)),
                         ">".join(enabled_ratios(a)),
                         ("每 %d 分钟" % int(a.分段录制几分钟 or 0))
                         if a.是否分段录制 else "否",
                         a.主播唯一码, a.用户id, a.房间号, a.抖音号,
                         a.直播地址, a.直播标题, a.备注, a.最后监控时间])
        header = ["序号", "主播名称", "直播状态", "在线人数", "粉丝量", "点赞量",
                  "监控时间", "已录时间", "录制状态", "是否监控", "是否录制",
                  "录制内容", "录制质量", "分段录制", "主播唯一码", "用户id",
                  "房间号", "抖音号", "直播地址", "直播标题", "备注",
                  "最后监控时间"]
        try:
            write_xlsx(path, [("主播数据", header, rows)])
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(e))
            return
        self._log("已导出：%s" % path, "ok")
        if QMessageBox.question(
                self, "导出完成", "已导出到：\n%s\n\n是否打开？" % path,
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            os.startfile(path)  # noqa: S606

    def on_export_records(self):
        days = store.list_record_days()
        if not days:
            QMessageBox.information(self, "提示", "暂无采集记录")
            return
        day = days[0]
        if len(days) > 1:
            day, ok = QInputDialog.getItem(
                self, "选择日期", "要导出哪一天的采集记录？", days, 0, False)
            if not ok:
                return
        snaps = store.load_snapshots(day)
        if not snaps:
            QMessageBox.information(self, "提示", "%s 无记录" % day)
            return
        default = os.path.join(config.DATA_DIR, "采集记录_%s.xlsx" % day)
        path, _ = QFileDialog.getSaveFileName(
            self, "导出采集记录", default, "Excel 工作簿 (*.xlsx)")
        if not path:
            return
        rows = [s.to_row(i) for i, s in enumerate(snaps, start=1)]
        write_xlsx(path, [("采集记录", SNAPSHOT_COLUMNS, rows)])
        self._log("已导出记录：%s" % path, "ok")
        if QMessageBox.question(
                self, "导出完成", "已导出到：\n%s\n\n是否打开？" % path,
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            os.startfile(path)  # noqa: S606

    def on_export_checked_txt(self):
        """对应原软件 导出直播间列表勾选数据TXT。"""
        items = self._checked() or self.anchors
        if not items:
            return
        default = os.path.join(
            config.DATA_DIR, "列表勾选数据_%s.txt" % time.strftime("%Y%m%d"))
        path, _ = QFileDialog.getSaveFileName(
            self, "导出列表勾选数据", default, "文本文件 (*.txt)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            for i, a in enumerate(items, start=1):
                f.write("%d\t%s\t%s\t%s\t%s\n"
                        % (i, a.主播名称, a.抖音号, a.主播唯一码, a.live_url))
        self._log("已导出勾选数据：%s" % path, "ok")

    # ---- 设置
    def on_settings(self):
        dlg = SettingsDialog(self, self.cfg)
        cfg = dlg.exec_get_config()
        if cfg:
            self.cfg = cfg
            config.save_config(cfg)
            self._log("设置已保存", "ok")

    def on_anchor_settings(self):
        items = self._selected()
        if not items:
            return
        dlg = AnchorSettingsDialog(self, items[0], self.cfg)
        if dlg.exec_get_anchor() is not None:
            self.model.refresh_volatile()
            self._save()
            self._log("「%s」独立设置已保存" % items[0].主播名称, "ok")

    def on_split_rec_setting(self, batch=False):
        items = self._targets(batch)
        if not items:
            return
        a = items[0]
        from PySide6.QtWidgets import QDialogButtonBox, QCheckBox, QSpinBox
        dlg = QDialog(self)
        dlg.setWindowTitle("分段录制设置（%d 个主播）" % len(items))
        lay = QVBoxLayout(dlg)
        cb = QCheckBox("启用分段录制", dlg)
        cb.setChecked(bool(a.是否分段录制))
        lay.addWidget(cb)
        lay.addWidget(QLabel("每段时长（分钟）：", dlg))
        sp = QSpinBox(dlg)
        sp.setRange(1, 720)
        sp.setValue(int(a.分段录制几分钟 or 30))
        lay.addWidget(sp)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, dlg)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        for x in items:
            x.是否分段录制 = cb.isChecked()
            x.分段录制几分钟 = sp.value()
        self.model.refresh_volatile()
        self._save()
        self._log("已设置 %d 个主播分段录制：%s"
                  % (len(items), ("每 %d 分钟" % sp.value())
                     if cb.isChecked() else "关闭"), "ok")

    def on_quality_setting(self, batch=False):
        """录制清晰度与优先顺序（对应原软件 直播录制质量 字典）。"""
        items = self._targets(batch)
        if not items:
            return
        from .dialogs import QualityChecks
        dlg = QDialog(self)
        dlg.setWindowTitle("录制清晰度与优先顺序")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "勾选允许录制的清晰度，按从左到右的顺序取流\n"
            "（取不到时自动回退到下一档）：", dlg))
        w = QualityChecks(dlg, items[0].直播录制质量)
        lay.addWidget(w)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, dlg)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        q = w.value()
        for a in items:
            a.直播录制质量 = dict(q)
        self.model.refresh_volatile()
        self._save()
        self._log("已设置 %d 个主播清晰度：%s"
                  % (len(items), ">".join(enabled_ratios(items[0])) or "无"),
                  "ok")

    def on_reset_elapsed(self, batch=False):
        items = self._targets(batch)
        if not items:
            return
        for a in items:
            a.已录时间 = 0
        self.model.refresh_volatile()
        self._log("已录时间已归零（%d 个）" % len(items))

    def on_reset_elapsed_restart(self, batch=False):
        items = self._targets(batch)
        if not items:
            return
        for a in items:
            a.已录时间 = 0
            if self.monitor.recorder.is_recording(a.key):
                self.monitor.recorder.stop(a.key)
                a.是否正在录制 = False
                a.录制状态 = "已停止"
        self.model.refresh_volatile()
        self._log("已录时间归零，重启录制将在下一轮检测时进行（%d 个）"
                  % len(items), "ok")

    # ---- 复制
    def on_copy_url(self):
        items = self._selected()
        if not items:
            return
        text = "\n".join(a.live_url for a in items if a.live_url)
        QApplication.clipboard().setText(text or "（无可用地址）")
        self._log("已复制 %d 条直播网址" % len(items), "ok")

    def on_copy_all_url(self):
        text = "\n".join(a.live_url for a in self.anchors if a.live_url)
        QApplication.clipboard().setText(text or "（无可用地址）")
        self._log("已复制全部 %d 条直播网址" % len(self.anchors), "ok")

    def on_copy_stream(self):
        """复制直播源（flv 流地址，需要联网取）。"""
        items = self._selected()
        if not items:
            return
        a = items[0]
        self._pending_stream = a
        self._log("正在获取「%s」的直播源…" % a.主播名称)

        import threading

        def work():
            room = self._current_room(a)
            if not room:
                self.bridge.logged.emit("warn", "「%s」当前未开播" % a.主播名称)
                return
            url = room.stream_for(primary_ratio(a)) or ""
            self.bridge.stream_ready.emit(a.key, url)

        threading.Thread(target=work, daemon=True).start()

    def _on_stream_ready(self, key, url):
        if url:
            QApplication.clipboard().setText(url)
            self._log("已复制直播源：%s" % url[:80], "ok")
        else:
            self._log("未取到直播源地址", "warn")

    def on_copy_data(self):
        items = self._selected()
        if not items:
            return
        lines = []
        for a in items:
            lines.append("\t".join(str(x) for x in [
                a.主播名称, a.是否开播, a.在线人数, a.粉丝量, a.点赞量,
                a.主播唯一码, a.房间号, a.抖音号, a.直播标题, a.live_url]))
        QApplication.clipboard().setText("\n".join(lines))
        self._log("已复制 %d 个主播的数据" % len(items), "ok")

    # ---- 打开目录
    def on_show_log(self):
        self._logwin = LogWindow(self)
        self._logwin.show()

    def _current_room(self, anchor):
        sec = DouyinLive.parse_sec_uid(anchor.主播唯一码) or anchor.主播唯一码
        if not sec:
            return None
        try:
            room, _u = self._client.live_of_user(sec)
        except DouyinError as e:
            self.bridge.logged.emit("error", "「%s」查询失败：%s"
                                    % (anchor.主播名称, e))
            return None
        return room

    def on_preview_stream(self):
        items = self._selected()
        if not items:
            QMessageBox.information(self, "提示", "请先选择主播")
            return
        a = items[0]
        self._log("正在获取「%s」的流地址…" % a.主播名称)

        import threading

        def work():
            room = self._current_room(a)
            if not room:
                self.bridge.logged.emit(
                    "warn", "「%s」当前未开播，无法预览" % a.主播名称)
                return
            url = room.stream_for(primary_ratio(a))
            preview_stream(self, url)

        threading.Thread(target=work, daemon=True).start()

    def on_open_in_browser(self):
        if self.cfg.get("使用内置浏览器"):
            items = self._selected()
            if not items or not items[0].live_url:
                QMessageBox.information(self, "提示", "没有可用的直播间地址")
                return
            self._log("使用系统默认浏览器打开：%s" % items[0].live_url, "info")
        items = self._selected()
        n = 0
        for a in items:
            if a.live_url:
                webbrowser.open(a.live_url)
                n += 1
        if n:
            self._log("已在浏览器打开 %d 个直播间" % n, "ok")
        else:
            QMessageBox.information(self, "提示", "没有可用的直播间地址")

    def _open_dir(self, path):
        os.makedirs(path, exist_ok=True)
        os.startfile(path)  # noqa: S606

    def on_open_data_dir(self):
        self._open_dir(config.DATA_DIR)

    def on_open_video_dir(self):
        self._open_dir(self.cfg.get("录制目录") or config.VIDEO_DIR)

    def on_open_anchor_video(self):
        items = self._selected()
        target = items[0].video_dir if items else config.VIDEO_DIR
        self._open_dir(target)

    def on_open_anchor_info(self):
        """打开主播信息目录并选中文件（explorer /select）。"""
        os.makedirs(config.ANCHOR_DIR, exist_ok=True)
        try:
            subprocess.Popen('explorer /select,"%s"' % config.ANCHOR_FILE)
        except Exception:  # noqa: BLE001
            os.startfile(config.ANCHOR_DIR)  # noqa: S606

    # -------------------------------------------------- 保存 / 退出
    def _save(self):
        try:
            store.save_anchors(self.anchors)
        except Exception as e:  # noqa: BLE001
            self._log("保存主播信息失败：%s" % e, "error")

    def closeEvent(self, event):
        if self.monitor.running or self.monitor.recorder.active_count():
            if QMessageBox.question(
                    self, "确认退出",
                    "当前正在监控/录制，退出会停止所有录制。确定退出？",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
            ) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._timer.stop()
        self.monitor.stop()
        self._save()
        config.save_config(self.cfg)
        event.accept()


# ============================================================ 附属窗口

class LogWindow(QWidget):
    """运行日志窗口（对应原软件 Frm_ShowRunLogs）。"""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("运行日志")
        self.resize(880, 560)
        lay = QVBoxLayout(self)
        txt = QPlainTextEdit(self)
        txt.setReadOnly(True)
        txt.setFont(QFont("Consolas", 9))
        txt.setPlainText(store.tail_log(2000) or "（今日暂无日志）")
        lay.addWidget(txt)


class HotLiveDialog(QDialog):
    """热门直播（对应原软件从列表加入监控的流程）。"""

    rooms_ready = Signal(object)
    rooms_failed = Signal(str)

    def __init__(self, parent, client, anchors, cfg=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.setWindowTitle("热门直播")
        self.resize(1000, 560)
        self.client = client
        self.anchors = anchors
        self.cfg = cfg or {}
        self.added = 0
        self.rooms = []
        self.rooms_ready.connect(self._on_rooms,
                                 Qt.ConnectionType.QueuedConnection)
        self.rooms_failed.connect(self._on_failed,
                                  Qt.ConnectionType.QueuedConnection)

        lay = QVBoxLayout(self)
        self.tip = QLabel("正在获取热门直播…")
        lay.addWidget(self.tip)

        self.table = QTableView(self)
        self._m = HotModel(self)
        self.table.setModel(self._m)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table)

        bar = QWidget(self)
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addStretch(1)
        self.btn = QPushButton("加入监控", bar)
        self.btn.setEnabled(False)
        self.btn.clicked.connect(self._add_selected)
        bl.addWidget(self.btn)
        lay.addWidget(bar)

        import threading

        def work():
            try:
                rooms = self.client.hot_rooms(count=30)
            except DouyinError as e:
                self.rooms_failed.emit(str(e))
                return
            self.rooms_ready.emit(rooms)

        threading.Thread(target=work, daemon=True).start()

    def _on_rooms(self, rooms):
        self.rooms = rooms
        self._m.set_rooms(rooms)
        self.tip.setText("共 %d 条（可多选后点「加入监控」）" % len(rooms))
        self.btn.setEnabled(True)

    def _on_failed(self, msg):
        self.tip.setText("获取失败：%s" % msg)

    def _add_selected(self):
        rows = sorted({i.row() for i in
                       self.table.selectionModel().selectedRows()})
        if not rows:
            QMessageBox.information(self, "提示", "请先选择直播间")
            return
        exist = {a.主播唯一码 for a in self.anchors if a.主播唯一码}
        added = 0
        for r in rows:
            if not (0 <= r < len(self.rooms)):
                continue
            room = self.rooms[r]
            if not room.sec_uid or room.sec_uid in exist:
                continue
            a = Anchor(主播名称=room.nickname or "", 主播唯一码=room.sec_uid,
                       用户id=room.uid or "", 抖音号=room.display_id or "",
                       房间号=room.web_rid or room.room_id or "",
                       是否监控=True,
                       是否分段录制=bool(self.cfg.get("是否分段录制")),
                       分段录制几分钟=int(self.cfg.get("分段录制几分钟") or 0),
                       直播录制质量=normalize_ratio(self.cfg.get("默认录制质量")),
                       录制多少分钟停止=int(
                           self.cfg.get("录制多少分钟停止") or 0))
            a.添加时间 = time.strftime("%Y-%m-%dT%H:%M:%S")
            a.粉丝量 = room.follower_count or ""
            a.是否开播 = True
            a.在线人数 = room.user_count
            a.显示在线人数 = room.user_count_str or ""
            a.直播标题 = room.title or ""
            a.直播地址 = room.live_url or ""
            self.anchors.append(a)
            exist.add(room.sec_uid)
            added += 1
        self.added = added
        self.tip.setText("已加入 %d 个主播" % added)
        self.btn.setEnabled(False)


class HotModel(QAbstractTableModel):
    HEADERS = ["主播名称", "在线人数", "粉丝量", "直播标题"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rooms = []

    def set_rooms(self, rooms):
        self.beginResetModel()
        self.rooms = list(rooms)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rooms)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (orientation == Qt.Orientation.Horizontal
                and role == Qt.ItemDataRole.DisplayRole):
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        r = self.rooms[index.row()]
        c = index.column()
        if c == 0:
            return r.nickname or "-"
        if c == 1:
            return r.user_count_str or fmt_num(r.user_count)
        if c == 2:
            return fmt_num(r.follower_count)
        if c == 3:
            return r.title or ""
        return None
