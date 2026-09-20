from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol


DownloadMode = Literal["direct", "hls", "segment_list", "jpeg_sequence", "magnet", "sniff"]
ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]


class HttpSession(Protocol):
    """requests.Session 与 curl_cffi Session 的公共接口（两者细节签名不同，按鸭子类型处理）。"""

    def request(self, *args: Any, **kwargs: Any) -> Any:
        ...

    def close(self) -> None:
        ...

    def __enter__(self) -> Any:
        ...

    def __exit__(self, *exc: Any) -> Any:
        ...


@dataclass(slots=True)
class DownloadTask:
    url: str
    mode: DownloadMode
    output_dir: Path
    output_name: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    concurrency: int = 4
    timeout: int = 30
    retries: int = 2
    verify_ssl: bool = True
    combine_segments: bool = True
    # 浏览器指纹伪装（curl_cffi）：Cloudflare 等按 TLS/HTTP2 指纹拦截的站点需要开启。
    impersonate: bool = True
    # 页面源码里找不到 m3u8 时，是否允许启动本机 Chrome 抓包。
    use_browser: bool = True
    # 网页嗅探模式：优先选择的清晰度标签（例如 "1080p"），为空表示自动选最高清晰度。
    preferred_quality: str = ""
    # 网页嗅探模式：已经解析好的流地址，填了就跳过重新嗅探直接下载。
    selected_stream_url: str = ""
    # CDN 常做防盗链：请求头里没有 Referer 时用这个值补上（嗅探模式 = 页面地址）。
    referer: str = ""


@dataclass(slots=True)
class DownloadResult:
    success: bool
    output_path: Path | None = None
    message: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StreamVariant:
    """一路可下载的视频流：既可能是主播放列表里的一档清晰度，也可能是一个独立播放列表。"""

    url: str
    label: str = ""
    resolution: str = ""
    bandwidth: int = 0
    average_bandwidth: int = 0
    codecs: str = ""
    source: str = ""

    @property
    def height(self) -> int:
        if "x" not in self.resolution:
            return 0
        try:
            return int(self.resolution.rsplit("x", 1)[1])
        except ValueError:
            return 0

    @property
    def quality_key(self) -> str:
        return label_for_resolution(self.resolution) or self.label

    def describe(self) -> str:
        parts: list[str] = [self.label or self.quality_key or "默认"]
        if self.resolution:
            parts.append(self.resolution)
        if self.bandwidth:
            parts.append(f"{self.bandwidth / 1_000_000:.1f} Mbps")
        return " · ".join(parts)


@dataclass(slots=True)
class SniffResult:
    """网页嗅探结果：命中的播放列表，以及展开后的清晰度列表。"""

    page_url: str
    playlists: list[str] = field(default_factory=list)
    variants: list[StreamVariant] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    page_title: str = ""
    # 本次嗅探实际使用（并验证可用）的 Referer，下载阶段要沿用。
    referer: str = ""


def label_for_resolution(resolution: str) -> str:
    """把 1920x1080 这样的分辨率转成 1080p 标签。"""
    if "x" not in resolution:
        return ""
    try:
        height = int(resolution.rsplit("x", 1)[1])
    except ValueError:
        return ""
    return f"{height}p" if height else ""
