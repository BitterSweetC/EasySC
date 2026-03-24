# Screen Casting

这个项目提供两种实际使用方式：

- `exe` 桌面端：适合本地视频投屏、网页视频播放页解析、兼容播放页、桌面/窗口投屏
- 油猴插件版：适合在浏览器里直接从网页视频页面发起投屏，但仍然需要本机启动一个本地 bridge

如果你只是想直接上手，优先看下面这两节：

- [版本一：EXE 桌面端](#版本一exe-桌面端)
- [版本二：油猴插件版](#版本二油猴插件版)

## 使用前提

- Windows 10 / 11
- 电脑和电视/盒子在同一局域网
- 目标设备支持 `DLNA / UPnP`，或者电视自带浏览器可打开局域网网页

补充说明：

- `yt-dlp` 用于把“视频播放页 URL”解析成真实视频流地址
- `ffmpeg` 用于把不兼容的视频转成更容易被电视播放的格式，也常用于合并 DASH 分离音视频
- 对很多普通 MP4 直链，`ffmpeg` 不是必需
- 对 Bilibili 这类常见 DASH 页面，通常需要 `ffmpeg`

如果 `ffmpeg` 不在系统 `PATH` 中，程序还会继续尝试这些位置：

- `SCREEN_CASTING_FFMPEG` 环境变量
- `C:\ffmpeg\bin\ffmpeg.exe`
- `C:\Program Files\ffmpeg\bin\ffmpeg.exe`
- `C:\Program Files (x86)\bililive\ugc_assistant\...\ffmpeg.exe`

## 版本一：EXE 桌面端

### 适合什么场景

- 把本地视频文件直接投到电视
- 输入网络视频直链后直接推送
- 输入网页视频播放页地址，让程序先解析真实视频地址再投屏
- 电视 DLNA 兼容一般时，生成“兼容播放页”让电视浏览器播放
- 把电脑全部桌面、单个屏幕，或者当前窗口投到电视

### 启动方式

如果你已经打包好桌面端，直接运行：

```text
dist\ScreenCasting.exe
```

如果仓库里还没有这个文件，可以自己打包：

```powershell
.\build_exe.ps1
```

打包完成后会生成：

```text
dist\ScreenCasting.exe
```

开发环境下也可以直接启动源码版：

```powershell
python main.py
```

### 使用方法

#### 1. 视频投送

1. 打开 `ScreenCasting.exe`
2. 在“视频投送”页点击“扫描盒子”，选择同一局域网中的电视或盒子
3. 选择一种视频来源：
   - 点击“选择视频”：投本地文件
   - 点击“输入网址”：输入网络视频直链，或者直接输入视频播放页地址
4. 选择投送方式：
   - 点击“开始 DLNA 推送”：直接推到支持 DLNA 的设备
   - 点击“生成兼容播放页”：生成一个局域网播放页地址，再去电视浏览器打开
5. 如果程序生成了“兼容播放页地址”，可以直接复制该地址，在电视浏览器里打开播放

#### 2. 兼容投屏

1. 打开“兼容投屏”页
2. 根据需求选择以下模式之一：
   - “电脑全部投屏”
   - “投屏一个屏幕”
   - “投屏当前页面”
3. 启动后程序会生成一个“接收地址”
4. 在电视浏览器中打开该地址，即可开始投屏

### 使用建议

- 电视支持稳定的 DLNA 时，优先使用“开始 DLNA 推送”
- 如果电视盒子能被搜索到，但播放失败，优先改用“生成兼容播放页”
- 只想展示浏览器、PPT 或某个软件窗口时，优先用“投屏当前页面”
- 要把整个电脑画面投出去时，优先用“电脑全部投屏”

## 版本二：油猴插件版

### 这一版的组成

油猴插件版不是“只装一个脚本就能投屏”的纯前端方案，它实际由两部分组成：

- 浏览器里的油猴脚本：`web-video-cast.user.js`
- 电脑本地运行的 bridge：负责扫描电视、解析视频、代理媒体流并通过 DLNA 投送

实际链路是：

```text
浏览器油猴脚本 -> 本机 bridge -> 本机媒体 HTTP 服务 -> 同一局域网 DLNA 电视
```

### 启动方式

#### 方式 A：直接运行 bridge exe

如果已经打包好 bridge，直接启动：

```text
dist\ScreenCastingBridge.exe
```

这是给普通用户最省事的启动方式，推荐优先使用。

如果需要自己打包 bridge：

```powershell
.\build_bridge_exe.ps1
```

打包完成后会生成：

```text
dist\ScreenCastingBridge.exe
```

第一次真正启动 `ScreenCastingBridge.exe` 时，如果本地还没有可用 `ffmpeg`，程序会自动下载并放到：

```text
dist\.runtime\ffmpeg\ffmpeg.exe
```

如果只是想先检查打包结果，不想直接启动 bridge，可以运行：

```powershell
dist\ScreenCastingBridge.exe --self-check
```

#### 方式 B：使用仓库内现成启动脚本

如果你是从源码目录直接运行，可以用：

```text
start_tampermonkey_bridge.cmd
```

这个脚本会优先尝试：

- 自动寻找系统 Python
- 必要时创建项目虚拟环境
- 安装 `requirements.txt`
- 尝试准备本地 `ffmpeg`

首次运行时，如果需要自动补齐运行环境，脚本会把相关文件放到项目目录下的：

```text
.runtime\
```

#### 方式 C：开发环境手动启动

```powershell
python run_bridge.py
```

也可以：

```powershell
python main.py --bridge
```

默认 bridge 地址是：

```text
http://127.0.0.1:9527/
```

如果要自定义地址和端口：

```powershell
python run_bridge.py --host 127.0.0.1 --port 9527
```

### 油猴脚本安装方法

脚本文件在项目根目录：

```text
web-video-cast.user.js
```

安装方式任选一种：

1. 在 Tampermonkey 中新建脚本，把 `web-video-cast.user.js` 的内容粘贴进去保存
2. 直接把该文件拖进 Tampermonkey 扩展页面进行安装

### 使用方法

1. 先在电脑上启动 bridge
2. 确认浏览器访问 `http://127.0.0.1:9527/` 能打开 bridge 页面
3. 安装并启用 `web-video-cast.user.js`
4. 打开任意网页视频页面
5. 点击页面右下角“投屏”
6. 先点击“检测”，确认油猴脚本能连上本地 bridge
7. 再点击“扫描”，选择同一局域网中的电视
8. 在候选视频源中选一个地址
9. 点击“开始投屏”

### 候选源怎么选

- 优先选择脚本已经识别出的 `HLS`、`DASH`、`MP4/WebM` 直链
- `当前页面解析` 主要依赖 `yt-dlp` 或站点专项适配，优先级低于真实媒体直链
- 如果页面只有 `blob:` 地址，而脚本没有抓到背后的真实视频流，这类站点通常需要额外适配

更详细的专项说明见：

- `TAMPERMONKEY_CASTING.md`

## 从源码安装依赖

如果你不是直接使用 `exe`，而是从源码运行，先执行：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 限制说明

- 不是所有网站都能投屏。当前方案通常至少要满足下面之一：
  - 页面里直接暴露真实媒体地址，比如 `.m3u8`、`.mp4`、`.mpd`
  - `yt-dlp` 支持该播放页，能把页面 URL 解析成真实流
  - 当前站点已经做了专项适配
- 目标设备必须支持 `DLNA / UPnP` 媒体播放，或者能在电视浏览器里打开本机生成的兼容播放页
- DRM 视频通常无法投屏
- 某些站点需要 `Referer`、`Cookie` 等请求头才能正常拉流
- 电视兼容性不一致；如果 DLNA 直投失败，优先改用程序生成的 `player_url` / 兼容播放页
- 电视需要能访问电脑暴露出来的局域网媒体地址，防火墙拦截会直接导致播放失败

以下情况通常无法直接支持，或者需要额外适配：

- 页面只暴露 `blob:` 地址，但没有抓到背后的真实 `.m3u8/.mp4`
- 视频地址依赖临时签名、加密参数或额外鉴权流程
- 播放器放在更深层 `iframe` 或自定义 SDK 中，当前脚本没有抓到真实请求
- 使用 DRM / Widevine / EME 的正版平台

如果某个站点失败，优先这样排查：

1. 看候选源里有没有 `HLS`、`DASH`、`MP4/WebM`
2. 如果有，优先选这些直链，不要先选 `当前页面解析`
3. 如果只有 `当前页面解析`，说明这个站点当前主要依赖 `yt-dlp` 或专项适配
4. 如果面板里只有 blob 相关播放器而没有真实流，基本就需要专项适配

油猴脚本的高级设置里提供“导出调试 JSON”，遇到不支持的站点时可以导出当前页的候选源和播放器信息，便于继续适配。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m unittest test_tampermonkey_bridge.py -v
```
