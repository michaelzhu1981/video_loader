from __future__ import annotations

import time
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Protocol

from video_loader.models import (
    DownloadResult,
    DownloadTask,
    HttpSession,
    LogCallback,
    ProgressCallback,
)
from video_loader.utils import unique_path

if TYPE_CHECKING:
    import requests


class Downloader(Protocol):
    def download(
        self,
        task: DownloadTask,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
        cancel_event: Event,
    ) -> DownloadResult:
        ...


def request_with_retries(
    session: HttpSession,
    method: str,
    url: str,
    task: DownloadTask,
    log_callback: LogCallback,
    *,
    stream: bool = False,
) -> requests.Response:
    last_error: Exception | None = None
    attempts = max(1, task.retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(
                method,
                url,
                headers=task.headers,
                cookies=task.cookies,
                timeout=task.timeout,
                verify=task.verify_ssl,
                stream=stream,
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # requests raises several concrete exception types.
            last_error = exc
            if attempt < attempts:
                log_callback(f"重试 {attempt}/{task.retries}：{url}")
                time.sleep(min(2 * attempt, 5))
    raise RuntimeError(f"请求失败：{url}（{last_error}）")


def download_file(
    session: HttpSession,
    url: str,
    path: Path,
    task: DownloadTask,
    log_callback: LogCallback,
    cancel_event: Event,
    progress_callback: ProgressCallback | None = None,
    progress_start: float = 0.0,
    progress_span: float = 1.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = unique_path(path)
    response = request_with_retries(session, "GET", url, task, log_callback, stream=True)
    total = int(response.headers.get("content-length") or 0)
    downloaded = 0

    with target.open("wb") as file:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if cancel_event.is_set():
                raise RuntimeError("下载已取消")
            if not chunk:
                continue
            file.write(chunk)
            downloaded += len(chunk)
            if progress_callback and total:
                ratio = min(downloaded / total, 1.0)
                progress_callback(progress_start + ratio * progress_span, target.name)

    return target
