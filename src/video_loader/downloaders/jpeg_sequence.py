from __future__ import annotations

from pathlib import Path
from threading import Event
from urllib.parse import urlparse

from video_loader.downloaders.base import cleanup_segments, download_many, request_with_retries
from video_loader.downloaders.hls import parse_m3u8_segments
from video_loader.services.http_client import build_session
from video_loader.models import DownloadResult, DownloadTask, LogCallback, ProgressCallback
from video_loader.services.ffmpeg import combine_with_concat_protocol
from video_loader.utils import safe_filename, unique_path


class JpegSequenceDownloader:
    def download(
        self,
        task: DownloadTask,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
        cancel_event: Event,
    ) -> DownloadResult:
        try:
            task.output_dir.mkdir(parents=True, exist_ok=True)
            with build_session(task, log_callback) as session:
                log_callback(f"正在获取 JPEG 播放列表：{task.url}")
                response = request_with_retries(session, "GET", task.url, task, log_callback)
                segments = parse_m3u8_segments(response.text, task.url, (".jpeg", ".jpg"))
                if not segments:
                    raise RuntimeError("播放列表中没有找到 JPEG 片段。")
                log_callback(f"找到 {len(segments)} 个 JPEG 片段。")

                files = download_many(
                    [
                        (url, task.output_dir / f"Video{index}{Path(urlparse(url).path).suffix or '.jpeg'}")
                        for index, url in enumerate(segments)
                    ],
                    task,
                    log_callback,
                    cancel_event,
                    progress_callback,
                    progress_span=0.9,
                    label="JPEG 片段",
                )

            output_name = safe_filename(task.output_name, "video.mp4") if task.output_name else "video.mp4"
            if not Path(output_name).suffix:
                output_name = f"{output_name}.mp4"
            output_path = unique_path(task.output_dir / output_name)
            combine_with_concat_protocol(files, output_path, log_callback)
            cleanup_segments(files, output_path, log_callback)
            progress_callback(1.0, "JPEG 图片序列下载完成")
            return DownloadResult(True, output_path, f"已保存到 {output_path}")
        except Exception as exc:
            return DownloadResult(False, None, "JPEG 图片序列下载失败", [str(exc)])
