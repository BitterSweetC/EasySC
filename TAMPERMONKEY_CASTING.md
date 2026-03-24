# 油猴投屏说明

这个方案是“两段式”：

1. 油猴脚本运行在网页里，负责收集当前页面的候选视频源
2. 本地桥接服务运行在电脑上，负责扫描 DLNA 电视、代理视频流、必要时本地合并音视频，并下发播放指令

原因很直接：浏览器脚本本身不能稳定完成局域网 SSDP / UPnP 发现，也不适合直接承担电视兼容代理。

## 运行前提

- 浏览器和本地 bridge 必须运行在同一台电脑上
- 电视或盒子必须支持 DLNA / UPnP
- 电脑和电视必须在同一局域网
- 需要先安装 Python 依赖：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

- 对很多网页播放页解析，需要 `yt-dlp`
- 对 Bilibili 这类 DASH 分离音视频页面，通常还需要单独安装 `ffmpeg`

## 启动桥接服务

推荐用独立 bridge 入口：

```powershell
python run_bridge.py
```

如果你想显式指定端口，也可以：

```powershell
python run_bridge.py --host 127.0.0.1 --port 9527
```

主入口也支持 bridge 模式：

```powershell
python main.py --bridge
```

启动后访问：

```text
http://127.0.0.1:9527/
```

## 安装油猴脚本

脚本文件在项目根目录：

```text
web-video-cast.user.js
```

安装方式任选一种：

1. 在 Tampermonkey 新建脚本，把文件内容粘贴进去保存
2. 把脚本文件拖进 Tampermonkey 扩展页安装

## 使用流程

1. 打开网页视频页面
2. 点击右下角“投屏”
3. 先点“检测”，确认能连上本地 bridge
4. 点“扫描”，选择同一局域网里的电视
5. 在候选视频源里选一个地址
6. 点“开始投屏”

对于需要本地下载和合并的长视频，油猴脚本会走异步任务模式：先提交投屏任务，再轮询 bridge 状态，所以不应该再因为浏览器侧单次请求超时而直接失败。

## 当前候选源提取逻辑

脚本会按优先级收集候选源：

1. 当前页面解析
2. 站点专用候选
3. 页面上的 `<video>` / `<source>`
4. `ld+json`
5. 资源请求记录和运行时抓到的 `fetch` / `xhr`

目前额外加强了这两类站点：

- Bilibili：尝试从 `__playinfo__`、`__INITIAL_STATE__` 和内联脚本中提取候选 URL
- 腾讯视频：尝试从 `__PLAYER_CONFIG__`、`VIDEO_INFO`、`COVER_INFO` 和内联脚本中提取候选 URL

即使没提取到直链，脚本也会保留“当前页面解析”这个候选项，让本地桥接继续走 `yt-dlp` 解析。

对于像 Bilibili 这种只暴露分离 DASH 音视频流的页面，bridge 会优先挑选 H.264 视频轨和 AAC 音轨，先下载到本地再用 `ffmpeg` 合并成兼容 MP4，然后再按原有 DLNA 链路投出去。

## 当前限制

- 目标设备需要支持 DLNA / UPnP 媒体播放
- DRM 视频通常无法投屏
- 某些站点需要 `Referer`、`Cookie` 才能拉流，脚本会把当前页面的常见请求头传给本地 bridge
- 电视播放器兼容性不一致；如果 DLNA 直投失败，可以继续使用 bridge 返回的 `player_url`
- 如果电视无法访问电脑的局域网媒体地址，通常是系统防火墙或路由隔离导致的

## 相关文件

- `main.py`: GUI / bridge 双入口
- `tampermonkey_bridge.py`: 本地桥接服务
- `run_bridge.py`: 独立 bridge 启动入口
- `web-video-cast.user.js`: 油猴脚本
