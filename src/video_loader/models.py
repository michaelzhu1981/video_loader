from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


DownloadMode = Literal["direct", "hls", "segment_list", "jpeg_sequence", "magnet"]
ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]


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


@dataclass(slots=True)
class DownloadResult:
    success: bool
    output_path: Path | None = None
    message: str = ""
    errors: list[str] = field(default_factory=list)
