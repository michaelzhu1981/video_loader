# 视频下载器

这是一个 Python 桌面下载工具，支持直链文件、HLS/m3u8 播放列表、手动粘贴的片段列表、旧脚本中的 JPEG 图片序列视频格式，以及磁力链接（magnet）。

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

## 界面选项

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
