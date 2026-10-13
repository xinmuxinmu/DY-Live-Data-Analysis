# DY Live Data Analysis

**DY Live Data Analysis** — a Python + PySide6 reimplementation of the
"DY直播数据分析" desktop tool: monitor Douyin live rooms, auto-record
streams, and export data.

---

## 功能 / Features

- **主播管理** — 添加 / 批量导入（抖音号、主页链接、唯一码、直播间链接四种输入自动识别）
- **开播监控** — 按间隔轮询，检测开播 / 关播 / 在线人数变化
- **自动录制** — 两种录制方式：
  - `录制流`（ffmpeg）→ `.ts` / `.mp4`，播放器直接播放
  - `下载流`（内置 FLV 下载器）→ `.flv`，**断线自动续传**，只写完整 FLV tag，
    任何时刻中断文件都不损坏
  - 支持分段录制、录满 N 分钟自动停止、视频 / 音频两种录制内容、一键停止全部
- **清晰度** — 蓝光 / 原画 / 超清 / 高清 / 标清，可选并排优先顺序
- **提醒与推送** — 开播 / 关播 / 开始录制 / 停止录制 / 在线人数提示音；
  企业微信 / 钉钉 / 飞书 / 自定义 Webhook 推送
- **数据导出** — 主播数据 Excel、采集记录 Excel（内置零依赖 xlsx 生成器）
- **数据统计** — QtCharts 柱状图 + 采集明细
- **视频转码** — 无损转 MP4、压缩、提取 MP3（ffmpeg）
- **直播预览** — ffplay 直接播放当前流地址
- **运行日志** — 按天记录，界面内查看

界面用 `QAbstractTableModel` + `QTableView`，主列表列名 / 顺序与原软件一致。

## 环境要求 / Requirements

| 项 | 说明 |
|---|---|
| Python | 3.8 或更高（安装时勾选 *Add Python to PATH*） |
| PySide6 | `pip install PySide6-Essentials`（界面需要，Addons 不必装） |
| ffmpeg | 可选但推荐。「录制流」录制、直播预览、视频转码需要 |
| 其它依赖 | **无**。除 PySide6 外全部使用 Python 标准库 |

## 运行 / Quick start

```bash
pip install PySide6-Essentials     # 国内慢可加 -i https://mirrors.aliyun.com/pypi/simple/

python main.py                     # GUI
```

Windows 也可以直接双击 `启动.bat` —— 它会在 PATH 上逐个探测
「能真正 `import PySide6`」的解释器，并优先用 `pythonw.exe` 启动（无控制台窗口）。

命令行模式：

```bash
python main.py cli hot -n 30       # 抓取热门直播
python main.py cli user XINMU__    # 按抖音号查主播
python main.py cli selftest        # a_bogus 签名算法自检
```

### 安装 ffmpeg

仓库不附带 ffmpeg 二进制（约 101 MB，且 GPL 软件不宜混在源码里再分发）。
把 `ffmpeg.exe` / `ffplay.exe` / `ffprobe.exe` 放到下面**任意一处**即可：

- `程序目录/ffmpeg/`
- 系统 `PATH`
- 环境变量 `DY_FFMPEG` 指向完整路径

下载：<https://www.gyan.dev/ffmpeg/builds/> ·
<https://github.com/BtbN/FFmpeg-Builds/releases> · <https://ffmpeg.org/download.html>

> 「下载流」录制方式不依赖 ffmpeg；ts/mp4 录制、直播预览、视频转码需要。

## 目录结构 / Layout

```
main.py              入口（GUI + CLI）
启动.bat              Windows 启动器（纯 ASCII，自动挑选解释器）
dy/                  核心模块（纯标准库）
  abogus.py          a_bogus 签名算法（Python 等价实现）
  abogus_origin.js   原实现 JS 参考（逆向自原软件，仅供比对）
  douyin.py          接口客户端
  monitor.py         监控主循环
  recorder.py        录制：ffmpeg 录制流 + FLV 下载流
  flv_downloader.py  下载流：FLV 流断点续传下载
  store.py           本地持久化
  models.py          数据模型
  xlsx.py            零依赖 xlsx 生成器
  config.py          路径与全局配置
ui/                  界面（PySide6 / Qt Widgets）
使用说明.txt          使用说明
迁移说明.md           移植说明（接口细节、录制方式、已知限制）
```

运行期数据（主播信息、录制文件、日志、配置）全部在 `软件数据/` 下自动生成，
已在 `.gitignore` 中排除。主播列表文件 `_LiveRoomInfo.txt` 为 27 字段，
与原软件的数据文件逐字段同构，可直接互换。

## 已知限制 / Known limitations

- 直播标题：主播主页接口不返回标题，只有「热门直播」路径有标题
- `enter` 接口被抖音风控拦截（返回空响应），需改用抖音号或主页链接
- 高频请求会触发风控，监控间隔建议 ≥ 30 秒
- 原软件本身不采集弹幕 / 礼物 / 榜单，本实现同样不包含
- 未移植付费功能：VIP 授权、语音转写、录制文件上传 OSS、极速下载（aria2c）

## 免责声明 / Disclaimer

本项目为学习与研究用途的**等价重写**，接口与实现均通过公开可访问的
HTTP 接口完成。请遵守目标平台的服务条款与 robots 规范，合理控制请求频率，
不要用于任何侵犯他人权益或商业利益的用途。使用本项目产生的一切后果由使用者自行承担。

## License

未指定 License —— 保留所有权利。如需开源，请自行添加。
