# Screen Casting

当前推荐版本：油猴脚本 + 本地 bridge  
当前油猴脚本版本：`0.5.9`

这是一个面向 Windows 的局域网投屏项目。按照当前仓库的实际情况，最推荐的用法是：

- 浏览器里安装油猴脚本 `web-video-cast.user.js`
- 电脑上启动本地桥接服务 `ScreenCastingBridge.exe`
- 由 bridge 负责扫描 DLNA 电视/盒子、解析网页视频、代理媒体流并发起投屏

仓库里也保留了桌面 GUI 方案，适合本地文件投屏、兼容播放页，以及桌面/窗口投屏，但当前仓库现成可直接使用的成品主要是桥接版 `ScreenCastingBridge.exe`。

## 当前仓库里有什么

- `ScreenCastingBridge.exe`
  当前仓库已经带了桥接版可执行文件，优先直接运行这个。
- `web-video-cast.user.js`
  油猴脚本，负责在网页里提取候选视频源并调用本地 bridge。
- `start_tampermonkey_bridge.cmd`
  源码目录下一键启动 bridge 的脚本，适合不想手动配 Python 环境时使用。
- `main.py`
  主入口。默认启动桌面 GUI；加 `--bridge` 时启动本地 bridge。

## 使用前提

- Windows 10 / 11
- 浏览器、油猴脚本和本地 bridge 需要运行在同一台电脑上
- 电脑和电视/盒子在同一局域网
- 目标设备支持 `DLNA / UPnP`，或者电视自带浏览器可打开局域网页面
- 如果从源码运行，建议使用 Windows 标准安装版 `Python 3.11` 或 `3.12`

补充说明：

- `yt-dlp` 用于把视频播放页 URL 解析成真实视频流地址
- `ffmpeg` 用于本地转码、合并 DASH 分离音视频，或者生成更容易被电视播放的兼容媒体
- 对很多普通 MP4 直链，`ffmpeg` 不是必需
- 对 Bilibili 这类常见 DASH 页面，通常需要 `ffmpeg`
- `http://127.0.0.1:9527/` 只是本地 bridge 控制页；真正投屏时，电视实际访问的是电脑局域网 IP 上的媒体地址

如果没有可用 `ffmpeg`，项目会优先尝试这些位置：

- `SCREEN_CASTING_FFMPEG` 环境变量
- 项目目录下的 `.runtime\ffmpeg\ffmpeg.exe`
- 项目目录下的 `ffmpeg.exe`
- 系统 `PATH` 中的 `ffmpeg`
- `C:\ffmpeg\bin\ffmpeg.exe`
- `C:\Program Files\ffmpeg\bin\ffmpeg.exe`
- `C:\Program Files (x86)\bililive\ugc_assistant\...\ffmpeg.exe`

## 方案一：油猴脚本 + 本地 bridge（推荐）

### 适合什么场景

- 你想直接在浏览器打开网页视频后发起投屏
- 你希望从页面里自动识别 `.m3u8`、`.mp4`、`.mpd` 等候选源
- 你希望在页面直链不明显时，让本地 bridge 再用 `yt-dlp` 做一次解析
- 你主要需求是“网页视频投电视”，而不是“整个桌面投屏”

### 实际组成

这不是“只装一个油猴脚本就能投屏”的纯前端方案，而是两段式架构：

- 浏览器油猴脚本：`web-video-cast.user.js`
- 本地桥接服务：`ScreenCastingBridge.exe` 或 `python run_bridge.py`

实际链路是：

```text
浏览器油猴脚本 -> 本机 bridge -> 本机媒体 HTTP 服务 -> 同一局域网 DLNA 电视/盒子
```

### 快速开始

#### 1. 先启动本地 bridge

优先使用当前仓库自带的可执行文件：

```powershell
.\ScreenCastingBridge.exe
```

默认启动地址：

```text
http://127.0.0.1:9527/
```

首次真正启动 `ScreenCastingBridge.exe` 时，如果本地没有现成 `ffmpeg`，程序会尝试自动下载并放到：

```text
.runtime\ffmpeg\ffmpeg.exe
```

如果你只是想先做自检，可以运行：

```powershell
.\ScreenCastingBridge.exe --self-check
```

如果你是从源码目录直接运行，也可以使用现成启动脚本：

```text
start_tampermonkey_bridge.cmd
```

这个脚本会尽量自动完成这些事情：

- 自动寻找系统 Python
- 必要时创建项目虚拟环境
- 安装 `requirements.txt`
- 尝试准备本地 `ffmpeg`

如果当前电脑没有可用 Python，这个脚本还会继续尝试：

- 下载便携版 Python（当前兜底范围是 `3.11` / `3.12`）
- 为便携版 Python 准备 `pip`

首次自动补齐环境时，相关文件会放到项目目录下的：

```text
.runtime\
```

首次运行时的联网行为说明：

- `ScreenCastingBridge.exe` 首次启动时，如果本地没有可用 `ffmpeg`，可能会自动下载 `ffmpeg`
- `start_tampermonkey_bridge.cmd` / `start_tampermonkey_bridge.ps1` 首次启动时，可能会联网下载便携版 Python、`get-pip.py`、`requirements.txt` 依赖和 `ffmpeg`
- 如果你处在公司网络、校园网或受限代理环境，首次启动失败时，优先检查网络放行、代理设置和杀毒/防火墙拦截

如果你是开发环境，手动启动 bridge 也可以：

```powershell
python run_bridge.py
```

或者：

```powershell
python main.py --bridge
```

如果你要改地址或端口：

```powershell
python run_bridge.py --host 127.0.0.1 --port 9527
```

油猴面板里的“本地 Bridge”地址也要同步改成对应值。

#### 关于端口和防火墙

- `9527` 负责本地控制页和 API，主要用于浏览器里的油猴脚本访问本机 bridge
- 真正开始投屏后，bridge 还会在电脑的局域网 IP 上临时启动一个媒体 HTTP 服务，端口通常是系统分配的随机端口，不固定
- 电视或盒子访问的通常是 `http://你的电脑局域网IP:随机端口/...` 这样的地址，而不是 `127.0.0.1:9527`
- 如果油猴面板能连上 bridge、也能扫到设备，但电视打不开 `player_url` 或开始播放后立刻断开，优先检查 Windows 防火墙、路由器 AP 隔离/访客网络隔离，以及电视是否真的能访问电脑的局域网地址

#### 2. 安装油猴脚本

脚本文件就在项目根目录：

```text
web-video-cast.user.js
```

安装方式任选一种：

1. 在 Tampermonkey 里新建脚本，把 `web-video-cast.user.js` 的内容粘贴进去保存
2. 直接把 `web-video-cast.user.js` 拖进 Tampermonkey 扩展页安装

#### 3. 开始投屏

1. 确认浏览器访问 `http://127.0.0.1:9527/` 能打开本地 bridge 页面
2. 打开任意网页视频页面
3. 点击页面右下角“投屏”
4. 先点“检测”，确认油猴脚本能连上本地 bridge
5. 再点“扫描电视”，选择同一局域网中的电视或盒子
6. 在候选视频源里选一个地址
7. 点击“开始投屏”

当前油猴面板里的相关选项状态：

- 画质可选 `最高可用`、`1080p`、`720p`
- 投屏模式当前建议使用 `原样优先` 或 `画质优先`
- `稳定 30fps` 选项目前在脚本里已暂时停用
- 注意：`画质优先` 遇到远端 `HLS(.m3u8)` 时，可能需要先在本地生成兼容 `MP4` 才能投屏；长视频、慢源或不兼容编码时，准备时间可能会明显变长。想尽快开始播放时，优先使用 `原样优先`，只有电视直投失败或播放不稳时再尝试 `画质优先`

### 候选视频源怎么选

- 优先选脚本已经识别出的真实媒体直链，例如 `HLS`、`DASH`、`MP4/WebM`
- `当前页面解析` 是回退方案，主要依赖 `yt-dlp` 或站点适配
- 如果页面只暴露 `blob:` 地址，而脚本没有抓到背后的真实流地址，这类站点通常还需要额外适配
- 对像 Bilibili 这种分离 DASH 音视频流页面，bridge 会优先在本地下载并合并成兼容 MP4 后再投出去

### 这个方案当前已经做了什么

- 油猴脚本会扫描页面里的 `<video>`、`<source>`、`ld+json`、网络请求记录以及运行时抓到的 `fetch/xhr`
- 对需要本地下载、合并或转码的长任务，脚本会先提交投屏任务，再轮询 bridge 状态，避免浏览器侧单次请求超时
- 即使没提取到直链，也会保留“当前页面解析”入口，让本地 bridge 继续调用 `yt-dlp`
- 当前脚本对 Bilibili、腾讯视频做了额外候选源提取增强
- 如果电视对直链兼容一般，bridge 返回的 `player_url` 可以作为兼容播放页备用地址

### 失败时优先这样排查

1. 先访问 `http://127.0.0.1:9527/`，确认 bridge 已经起来
2. 在油猴面板里先点“检测”，再点“扫描电视”
3. 候选源里如果有真实直链，优先选直链，不要先选“当前页面解析”
4. 如果 DLNA 直投失败，但返回了 `player_url`，优先改用那个兼容播放页
5. 如果是新站点或奇怪页面，导出油猴面板里的“调试 JSON”继续分析

更详细的专项说明见：

- `TAMPERMONKEY_CASTING.md`

## 方案二：桌面 GUI（可选）

### 适合什么场景

- 直接投本地视频文件
- 输入网络视频直链或播放页地址，让程序先解析再投
- 电视对 DLNA 兼容一般时，生成兼容播放页让电视浏览器播放
- 把电脑全部桌面、单个屏幕，或者当前窗口投到电视

### 当前仓库怎么启动

桌面 GUI 入口还在源码里，但当前仓库没有现成的 `ScreenCasting.exe` 成品文件。现在有两种方式：

源码直接运行：

```powershell
python main.py
```

自己打包桌面版：

```powershell
.\build_exe.ps1
```

如果你修改的是 bridge 代码，需要重新打包桥接版时再执行：

```powershell
.\build_bridge_exe.ps1
```

### 桌面 GUI 能做什么

- 视频投送：
  支持本地文件、网络视频直链、视频播放页地址解析，再通过 DLNA 直推或生成兼容播放页
- 兼容投屏：
  支持电脑全部投屏、单个屏幕投屏、当前窗口投屏，并生成电视浏览器可访问的接收地址

### 使用建议

- 电视支持稳定的 DLNA 时，优先使用“开始 DLNA 推送”
- 设备能被扫描到但播放失败时，优先改用“生成兼容播放页”
- 只想展示浏览器、PPT 或某个软件窗口时，优先用窗口/页面投屏
- 要投整个电脑画面时，再用桌面投屏

## 从源码运行时的依赖安装

如果你不是直接运行仓库里现成的 `ScreenCastingBridge.exe`，而是准备从源码跑：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

说明：

- 建议使用 Windows 标准安装版 `Python 3.11` 或 `3.12`
- `requirements.txt` 当前只包含 `Pillow` 和 `yt-dlp`
- `tkinter` 依赖标准 Windows Python 安装，一般不通过 `pip` 单独安装
- 如果系统里只有 Microsoft Store 的 Python 命令别名，建议直接安装标准版 Python，或者改用 `start_tampermonkey_bridge.cmd` 让项目自行准备运行环境

## 限制说明

- 不是所有网站都能投屏，通常至少要满足下面之一：
  - 页面里直接暴露真实媒体地址，例如 `.m3u8`、`.mp4`、`.mpd`
  - `yt-dlp` 支持该播放页 URL
  - 当前站点已经做了专项适配
- 目标设备必须支持 `DLNA / UPnP` 媒体播放，或者能在电视浏览器里打开本机生成的兼容播放页
- DRM 视频通常无法投屏
- 某些站点依赖 `Referer`、`Cookie` 或其他请求头，bridge 会转发常见页面请求头，但不能保证所有站点都能成功
- 如果电视无法访问电脑暴露出来的媒体地址，通常是系统防火墙、路由器隔离或局域网策略导致的；注意这里不只涉及 `9527`，还涉及投屏时动态创建的媒体端口

以下情况通常无法直接支持，或者仍需要继续适配：

- 页面只暴露 `blob:` 地址，没有抓到背后的真实流
- 视频地址依赖临时签名、加密参数或额外鉴权流程
- 播放器藏在更深层 `iframe` 或自定义 SDK 内，当前脚本没有抓到真实请求
- 使用 DRM / Widevine / EME 的正版平台

## 相关文件

- `web-video-cast.user.js`：油猴脚本
- `tampermonkey_bridge.py`：本地 bridge 核心服务
- `bridge_launcher.py`：桥接版 exe 入口
- `run_bridge.py`：开发环境桥接启动入口
- `main.py`：桌面 GUI / bridge 双入口
- `TAMPERMONKEY_CASTING.md`：油猴投屏专项说明

## 测试

当前仓库里可直接运行的测试入口是：

```powershell
python -m unittest test_tampermonkey_bridge.py -v
```

## License

Apache License 2.0，见 `LICENSE.txt`。
