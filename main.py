# -*- coding: utf-8 -*-
"""DY直播数据分析（Python 版）—— 启动入口

原软件：DY直播数据分析（.NET / WinForms / DNGuard HVM 保护）
本版本：按原软件功能与界面结构，用 Python + PySide6 等价重写。

用法：
    python main.py                 启动图形界面
    python main.py cli ...         命令行模式（见 --help）
"""
from __future__ import annotations

import os
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

APP_NAME = "DY直播数据分析"


# --------------------------------------------------------------- 崩溃处理

def _write_crash_log(text):
    """把异常写入日志文件，返回路径。pythonw 无控制台，必须落盘。"""
    try:
        from dy import config
        os.makedirs(config.LOG_DIR, exist_ok=True)
        import time
        path = os.path.join(config.LOG_DIR, "启动错误.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n===== %s =====\n%s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        return path
    except Exception:  # noqa: BLE001
        return ""


def _report_fatal(text):
    log_path = _write_crash_log(text)
    msg = text
    if log_path:
        msg += "\n\n详细信息已写入：\n%s" % log_path
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance()
        owned = app is None
        if owned:
            app = QApplication(sys.argv)
        QMessageBox.critical(None, "%s - 启动失败" % APP_NAME, msg)
        del owned
    except Exception:  # noqa: BLE001
        sys.stderr.write(msg + "\n")


# --------------------------------------------------------------- 命令行模式

def run_cli(argv):
    """无界面模式，便于脚本化 / 定时任务。"""
    from dy import config, store
    from dy.douyin import DouyinError, DouyinLive

    def usage():
        print("""%s（Python 版）命令行

  python main.py cli hot [-n 30] [-o 输出.xlsx]   采集热门直播并导出
  python main.py cli check                        检测所有已配置主播
  python main.py cli user <主页链接|sec_uid>       查询单个主播
  python main.py cli selftest                     签名算法自检
""" % APP_NAME)

    if not argv:
        usage()
        return 0
    cmd = argv[0]

    if cmd == "selftest":
        from dy import abogus
        FIXED = 1760000000000
        seed = [123456789]

        def rand():
            seed[0] = (seed[0] * 48271) % 2147483647
            return seed[0] / 2147483647

        qs = "aid=6383&app_name=douyin_web&web_rid=332506829704"
        got = abogus.a_bogus(qs, abogus.DEFAULT_UA, rand=rand,
                             now=lambda: FIXED)
        expect = ("dJmZQdLDDDIihDSv56nLfY3q6333Yh7l0trEMD2fedft0L39HMYD9exo1H"
                  "UveY8ji4%2FsIeYjy4hbT3ohrQ2y8qwf9W0L%2F25gsDSkKl12so0j53inC"
                  "Lf%2FE0iE5hsAtFH8svr4iKi8owICSYyhldAJ5kIlO62-zo0%2F9W8%3D")
        ok = got == expect
        print("a_bogus 自检:", "通过" if ok else "失败")
        if not ok:
            print("  期望:", expect)
            print("  实际:", got)
        return 0 if ok else 1

    cli = DouyinLive(log=lambda *a: print("[dy]", *a))

    if cmd == "hot":
        n = 15
        out = "热门直播.xlsx"
        if "-n" in argv:
            n = int(argv[argv.index("-n") + 1])
        if "-o" in argv:
            out = argv[argv.index("-o") + 1]
        rooms, seen, offset = [], set(), 0
        while len(rooms) < n:
            try:
                page = cli.hot_rooms(15, offset)
            except DouyinError as e:
                print("失败:", e)
                break
            if not page:
                break
            add = 0
            for r in page:
                k = r.room_id or r.web_rid
                if k and k not in seen:
                    seen.add(k)
                    rooms.append(r)
                    add += 1
            print("  第 %d 页 +%d，累计 %d" % (offset // 15 + 1, add, len(rooms)))
            offset += 15
            if not add:
                break
            cli.sleep()
        rooms = rooms[:n]
        from dy.models import snapshot_from_room
        from dy.xlsx import write_xlsx
        cols = ["序号", "主播名称", "抖音号", "主播唯一码", "在线人数",
                "显示在线人数", "累计观看", "粉丝量", "点赞量", "直播标题",
                "城市", "有商品", "直播地址", "采集时间"]
        rows = []
        for i, r in enumerate(rooms, 1):
            s = snapshot_from_room(r)
            rows.append([i, s.主播名称, r.display_id, s.主播唯一码,
                         s.在线人数, s.显示在线人数, s.累计观看, s.粉丝量,
                         s.点赞量, s.直播标题, s.城市, s.有商品, s.直播地址,
                         s.采集时间])
        path = os.path.join(BASE_DIR, out)
        write_xlsx(path, [("热门直播", cols, rows)])
        print("已导出 %s（%d 条）" % (path, len(rooms)))
        return 0

    if cmd == "check":
        anchors = store.load_anchors()
        if not anchors:
            print("主播列表为空")
            return 1
        from dy.monitor import Monitor
        mon = Monitor(lambda: anchors, config.load_config,
                      emit=lambda lv, t: print("  [%s] %s" % (lv, t)))
        for a in anchors:
            if a.是否监控:
                mon._check_one(a)
                cli.sleep()
        store.save_anchors(anchors)
        for a in anchors:
            print("  %-20s %-6s 在线=%s"
                  % (a.主播名称, "直播中" if a.是否开播 else "未开播",
                     a.显示在线人数))
        return 0

    if cmd == "user":
        if len(argv) < 2:
            usage()
            return 2
        sec = DouyinLive.parse_sec_uid(argv[1])
        if not sec:
            print("无法识别主播标识")
            return 2
        room, user = cli.live_of_user(sec)
        print("主播:", (user or {}).get("nickname"))
        print("粉丝:", (user or {}).get("follower_count"))
        if room:
            print("状态: 直播中  在线=%s  清晰度=%s"
                  % (room.user_count, "/".join(room.quality_list) or "-"))
        else:
            print("状态: 未开播")
        return 0

    usage()
    return 2


# --------------------------------------------------------------- 图形界面

def run_gui():
    from PySide6.QtWidgets import QApplication

    # Qt6 默认开启高 DPI 缩放，AA_UseHighDpiPixmaps 已废弃，无需再设置。
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)

    from ui.main_window import MainWindow
    win = MainWindow()
    win.show()
    return app.exec()


# --------------------------------------------------------------- 入口

def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("cli", "--cli"):
        return run_cli(argv[1:])
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv and argv[0] in ("selftest", "自检"):
        return run_cli(["selftest"])
    return run_gui()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        _report_fatal(traceback.format_exc())
        sys.exit(1)
