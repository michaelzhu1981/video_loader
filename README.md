# 视频下载器

这是一个 Python 桌面下载工具，支持直链文件、HLS/m3u8 播放列表、手动粘贴的片段列表，以及旧脚本中的 JPEG 图片序列视频格式。

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
- Python 依赖需要安装在项目的 `.venv` 虚拟环境中

macOS 使用 Homebrew 安装 ffmpeg：

```bash
brew install ffmpeg
```

## 下载方式

- `直链文件`：下载单个 URL，未填写输出文件名时使用远端文件名。
- `HLS / m3u8`：解析播放列表，下载媒体片段，然后用 ffmpeg 合并。
- `片段列表`：在 URL 输入框中按行粘贴多个片段 URL，可选择是否合并。
- `JPEG 图片序列`：兼容旧脚本逻辑，下载播放列表中的 `.jpeg` 或 `.jpg` 片段，然后用 ffmpeg 合并。

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
