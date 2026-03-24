# Screen Casting

这个项目不是“只装一个油猴脚本就能投屏”的纯前端工具。

实际运行链路是：

`浏览器油猴脚本 -> 本机 Python bridge -> 本机媒体 HTTP 服务 -> 同一局域网 DLNA 电视`

仓库里提供两种入口：

- `python main.py`
  桌面 GUI，适合本地文件投屏、兼容播放页、桌面镜像、Windows 投影辅助
- `python run_bridge.py`
  本地 Tampermonkey bridge，适合配合 `web-video-cast.user.js` 把网页视频投到电视

## 环境要求

- Python 3.10+
- 同一局域网中的 DLNA / UPnP 电视或盒子
- 浏览器安装 Tampermonkey
- 推荐 Windows 10 / 11

以下依赖也要区分清楚：

- `yt-dlp`
  已放进 `requirements.txt`，用于把网页播放页解析成真实媒体地址
- `ffmpeg`
  不是 Python 包，需要单独安装。对直接 MP4 链接不是必需，但对 Bilibili 这类 DASH 分离音视频页面通常必需

如果 `ffmpeg` 不在 `PATH`，程序会继续尝试这些位置：

- `SCREEN_CASTING_FFMPEG` 环境变量
- `C:\ffmpeg\bin\ffmpeg.exe`
- `C:\Program Files\ffmpeg\bin\ffmpeg.exe`
- `C:\Program Files (x86)\bililive\ugc_assistant\...\ffmpeg.exe`

## 安装

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 网页视频投屏快速开始

1. 启动本地 bridge：

```powershell
python run_bridge.py
```

默认地址：

```text
http://127.0.0.1:9527/
```

也可以使用主入口：

```powershell
python main.py --bridge
```

自定义监听地址和端口：

```powershell
python run_bridge.py --host 127.0.0.1 --port 9527
```

2. 安装油猴脚本：

- `web-video-cast.user.js`

3. 在网页视频页面点击右下角“投屏”，依次完成：

- 检测本地 bridge
- 扫描电视
- 选择候选视频源
- 开始投屏

详细说明见：

- `TAMPERMONKEY_CASTING.md`

## 本地 GUI 快速开始

```powershell
python main.py
```

GUI 模式可以做这些事：

- 扫描 DLNA 电视并投送本地文件
- 为远程视频生成兼容播放页
- 启动桌面镜像，并在电视浏览器中打开生成的局域网地址
- 调起 Windows 投影辅助功能

## 限制说明

- 目标设备必须支持 DLNA / UPnP 媒体播放
- DRM 视频通常无法投屏
- 某些站点需要 `Referer`、`Cookie` 等请求头才能正常拉流
- 电视兼容性不一致；如果 DLNA 直投失败，可以尝试程序返回的 `player_url`
- 电视需要能访问电脑暴露出来的局域网媒体地址，防火墙拦截会直接导致播放失败

## 测试

```powershell
python -m unittest discover -s tests -v
python -m unittest test_tampermonkey_bridge.py -v
```

## 打包 exe

先安装 `PyInstaller`，再运行：

```powershell
.\build_exe.ps1
```
