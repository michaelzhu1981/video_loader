from __future__ import annotations

from threading import Event

from video_loader.downloaders.direct import DirectDownloader
from video_loader.downloaders.hls import HlsDownloader
from video_loader.downloaders.jpeg_sequence import JpegSequenceDownloader
from video_loader.downloaders.segment_list import SegmentListDownloader
from video_loader.models import DownloadMode, DownloadResult, DownloadTask, LogCallback, ProgressCallback


class DownloadManager:
    def __init__(self) -> None:
        self._downloaders = {
            "direct": DirectDownloader(),
            "hls": HlsDownloader(),
            "segment_list": SegmentListDownloader(),
            "jpeg_sequence": JpegSequenceDownloader(),
        }

    def download(
        self,
        task: DownloadTask,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
        cancel_event: Event,
    ) -> DownloadResult:
        downloader = self._downloaders.get(task.mode)
        if not downloader:
            return DownloadResult(False, None, f"Unsupported download mode: {task.mode}", [])
        return downloader.download(task, progress_callback, log_callback, cancel_event)


def available_modes() -> dict[DownloadMode, str]:
    return {
        "direct": "直链文件",
        "hls": "HLS / m3u8",
        "segment_list": "片段列表",
        "jpeg_sequence": "JPEG 图片序列",
    }
