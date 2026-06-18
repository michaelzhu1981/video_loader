from __future__ import annotations

from pathlib import Path
from threading import Event

import requests

from video_loader.downloaders.base import download_file
from video_loader.models import DownloadResult, DownloadTask, LogCallback, ProgressCallback
from video_loader.utils import filename_from_url, safe_filename


class DirectDownloader:
    def download(
        self,
        task: DownloadTask,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
        cancel_event: Event,
    ) -> DownloadResult:
        try:
            task.output_dir.mkdir(parents=True, exist_ok=True)
            filename = safe_filename(task.output_name) if task.output_name else filename_from_url(task.url)
            target = task.output_dir / filename
            log_callback(f"正在下载直链文件：{task.url}")
            with requests.Session() as session:
                output = download_file(session, task.url, target, task, log_callback, cancel_event, progress_callback)
            progress_callback(1.0, "直链下载完成")
            return DownloadResult(True, output, f"已保存到 {output}")
        except Exception as exc:
            return DownloadResult(False, None, "直链下载失败", [str(exc)])
