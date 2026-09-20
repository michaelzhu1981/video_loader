from __future__ import annotations

from pathlib import Path
from threading import Event
from urllib.parse import urlparse

from video_loader.downloaders.base import cleanup_segments, download_many
from video_loader.models import DownloadResult, DownloadTask, LogCallback, ProgressCallback
from video_loader.services.ffmpeg import combine_with_concat_demuxer
from video_loader.utils import safe_filename, unique_path


class SegmentListDownloader:
    def download(
        self,
        task: DownloadTask,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
        cancel_event: Event,
    ) -> DownloadResult:
        try:
            urls = [line.strip() for line in task.url.splitlines() if line.strip() and not line.strip().startswith("#")]
            if not urls:
                raise RuntimeError("片段列表为空。")

            segment_dir = task.output_dir / "_segments"
            task.output_dir.mkdir(parents=True, exist_ok=True)
            segment_dir.mkdir(parents=True, exist_ok=True)
            files = download_many(
                [
                    (url, segment_dir / f"segment-{index:05d}{Path(urlparse(url).path).suffix or '.bin'}")
                    for index, url in enumerate(urls)
                ],
                task,
                log_callback,
                cancel_event,
                progress_callback,
                progress_span=0.9,
                label="片段",
            )

            if not task.combine_segments:
                progress_callback(1.0, "片段下载完成")
                return DownloadResult(True, segment_dir, f"片段已保存到 {segment_dir}")

            output_name = safe_filename(task.output_name, "video.mp4") if task.output_name else "video.mp4"
            if not Path(output_name).suffix:
                output_name = f"{output_name}.mp4"
            output_path = unique_path(task.output_dir / output_name)
            combine_with_concat_demuxer(files, output_path, log_callback)
            cleanup_segments(files, output_path, log_callback)
            progress_callback(1.0, "片段列表下载完成")
            return DownloadResult(True, output_path, f"已保存到 {output_path}")
        except Exception as exc:
            return DownloadResult(False, None, "片段列表下载失败", [str(exc)])
