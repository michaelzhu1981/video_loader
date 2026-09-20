# 视频下载器

这是一个 Python 桌面下载工具，支持直链文件、HLS/m3u8 播放列表、手动粘贴的片段列表、旧脚本中的 JPEG 图片序列视频格式、磁力链接（magnet），以及粘贴网页地址后自动识别 m3u8 视频并选择清晰度下载（网页嗅探）。

## 安装和启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m video_loader.app
```

完成安装后，也可以直接运行：

```bash
./run.sh
```

## 运行要求

- Python 3.11 或更高版本
- 合并 HLS、片段列表或 JPEG 图片序列时，需要 `ffmpeg`。如果系统和虚拟环境中都没有找到，程序会提示是否安装到当前 `.venv`。
- 下载磁力链接时需要 `aria2`（程序通过 `aria2c` 的 JSON-RPC 接口接管下载）。如果找不到，程序同样会提示自动安装。
- Python 依赖需要安装在项目的 `.venv` 虚拟环境中
- 网页嗅探用 `curl_cffi` 伪装浏览器 TLS/HTTP2 指纹（Cloudflare 等站点必须），浏览器抓包用 `websocket-client`，两者已在 `pyproject.toml` 里声明。
  升级依赖后请重新执行一次 `pip install -r requirements.txt`。
- 浏览器抓包需要本机安装 Chrome / Chromium / Brave / Edge 之一（默认查找 `/Applications/Google Chrome.app`）。

macOS 使用 Homebrew 安装 ffmpeg：

```bash
brew install ffmpeg
```

macOS 使用 Homebrew 安装 aria2：

```bash
brew install aria2
```

## 下载方式

- `直链文件`：下载单个 URL，未填写输出文件名时使用远端文件名。
- `HLS / m3u8`：解析播放列表，下载媒体片段，然后用 ffmpeg 合并。
- `片段列表`：在 URL 输入框中按行粘贴多个片段 URL，可选择是否合并。
- `JPEG 图片序列`：兼容旧脚本逻辑，下载播放列表中的 `.jpeg` 或 `.jpg` 片段，然后用 ffmpeg 合并。
- `磁力链接`：在 URL 输入框中按行粘贴 `magnet:?xt=urn:btih:...` 链接（以 `#` 开头的行是注释，会被忽略）。程序会为每个任务启动一个临时的 `aria2c` 进程（本地随机端口 + 随机密钥的 JSON-RPC），下载完成后自动结束进程，不需要额外安装 Python 依赖。链接会在本地先校验 `btih` 哈希，格式不对时立即报错；等待种子元信息期间日志每 5 秒输出一次连接数和种籽数。
- `网页嗅探`：在 URL 输入框中粘贴**视频页面地址**（不是 m3u8 链接），点「解析网页」；程序解析出所有清晰度填进「清晰度」下拉框，选择后点「开始」下载（内部按 HLS 处理：下载片段 + ffmpeg 合并）。

## 网页嗅探与反爬说明

嗅探分两步，按代价从小到大：

1. **页面源码扫描**：用伪装指纹的会话抓取页面 HTML，扫描其中的 m3u8 地址（支持 JS 转义写法 `https:\/\/host\/a.m3u8`、相对路径、以及一层 iframe）。
2. **浏览器抓包**：页面源码里没有（播放器运行时才拼出地址）或者页面被 Cloudflare 的 JS 挑战挡住时，自动启动一个临时 profile 的 Chrome，用 DevTools 协议监听播放器真正请求的媒体地址。
   - 抓包必须用**有窗口**的 Chrome：`--headless=new` 过不了 Cloudflare 的挑战。窗口会自动关闭，不会碰你自己的浏览器配置（临时 `user-data-dir`）。
   - 页面没有自动播放时需要手动点一下播放，或者直接用「HLS / m3u8」模式粘贴链接。

反爬的关键是 **TLS/HTTP2 指纹而不是请求头或 Cookie**。实测同一个 CDN：

| 客户端 | 结果 |
| --- | --- |
| `curl` + 完整浏览器头 + `Referer` + `cf_clearance` | 403 |
| `requests`（默认指纹） | 403 |
| `curl_cffi` `impersonate="chrome"` | 200 |

因此所有下载器都通过 `services/http_client.build_session()` 取会话：勾选「浏览器指纹伪装」时走 `curl_cffi`，未安装时回退 `requests` 并在日志里提示。

### 请求头的两个坑（都实测过，程序会自动兜住）

指纹伪装 ≠ 随便写请求头。同一个站点、同一份指纹，只有下面这组组合成功：

| 请求头 | 页面（missav.ai） | CDN（surrit.com） |
| --- | --- | --- |
| 什么都不加 | 403 | 403 |
| `User-Agent: Mozilla/5.0`（旧版默认预设） | 403 | 403 |
| 只加 `Referer` | 403 | 200 |
| `User-Agent: Mozilla/5.0` + `Referer` | 403 | 403 |
| 真实 Chrome UA + `Referer` | **200** | **200** |
| 真实 Chrome UA，无 `Referer` | 403 | 403 |

1. **`User-Agent` 必须和指纹一致**。写 `Mozilla/5.0` 这类占位 UA 会覆盖 curl_cffi 内置的 Chrome UA，Cloudflare 立刻 403。
   指纹伪装开启时程序会丢掉占位 UA 并在日志里说明；要自定义就写完整的浏览器 UA（预设「Chrome 桌面」）。
2. **`Referer` 常常是必填的**。嗅探模式会自动用页面地址作为 Referer，并沿用到播放列表和分段请求；
   直接用「HLS / m3u8」模式粘 CDN 直链时，需要自己在请求头里写 `Referer: https://站点/`。

个别站点（例如分段名是 `.jpeg` 但内容其实是 MPEG-TS 的源）可直接用 `JPEG 图片序列` 模式，或交给嗅探模式自动处理。

## 界面选项

- `解析网页` / `清晰度`：粘贴页面地址后点「解析网页」，下拉框会列出解析到的所有清晰度（默认选最高画质，格式 `1080p · 1920x1080 · 8.1 Mbps`）。
- `浏览器指纹伪装`：用 `curl_cffi` 伪装浏览器指纹，Cloudflare 一类按指纹拦截的站点必须开启（默认开启）。
- `源码里没有 m3u8 时启动浏览器抓包`：允许自动开 Chrome 抓包（默认开启）。如果不想弹浏览器窗口，可关闭它，此时只会扫描页面源码。
- `保存目录`：最终文件和临时片段的保存位置。
- `输出文件名`：可选的最终文件名；合并视频通常使用 `.mp4` 结尾。
- `并发数`：HLS 片段并行下载数量。
- `超时秒数`：单次网络请求超时时间。
- `重试次数`：请求失败后的重试次数。
- `校验 SSL 证书`：只有在来源证书异常时才建议关闭。
- `请求头和 Cookie`：可选请求信息；请求头每行一个，Cookie 使用 `key=value; key2=value2` 格式。

## 开发检查

```bash
source .venv/bin/activate
python -m compileall src
python -m unittest
```
