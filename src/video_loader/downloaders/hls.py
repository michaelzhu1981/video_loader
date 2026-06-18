from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event
from urllib.parse import urljoin, urlparse

import requests

from video_loader.downloaders.base import download_file, request_with_retries
from video_loader.models import DownloadResult, DownloadTask, LogCallback, ProgressCallback
from video_loader.services.ffmpeg import combine_with_concat_demuxer
from video_loader.utils import filename_from_url, safe_filename, unique_path


def parse_m3u8_segments(text: str, base_url: str, suffixes: tuple[str, ...] | None = None) -> list[str]:
    segments: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        url = urljoin(base_url, line)
        if suffixes:
            path = urlparse(url).path.lower()
            if not path.endswith(suffixes):
                continue
        segments.append(url)
    return segments


def is_playlist_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".m3u8")


class HlsDownloader:
    def download(
        self,
        task: DownloadTask,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
        cancel_event: Event,
    ) -> DownloadResult:
        try:
            task.output_dir.mkdir(parents=True, exist_ok=True)
            with requests.Session() as session:
                log_callback(f"正在获取播放列表：{task.url}")
                response = request_with_retries(session, "GET", task.url, task, log_callback)
                segments = parse_m3u8_segments(response.text, task.url)
                if segments and all(is_playlist_url(url) for url in segments):
                    child_playlist = segments[-1]
                    log_callback(f"正在跟进子播放列表：{child_playlist}")
                    response = request_with_retries(session, "GET", child_playlist, task, log_callback)
                    segments = parse_m3u8_segments(response.text, child_playlist)
                if not segments:
                    raise RuntimeError("播放列表中没有找到媒体片段。")

                log_callback(f"找到 {len(segments)} 个播放列表片段。")
                segment_dir = task.output_dir / "_segments"
                files = self._download_segments(segments, segment_dir, task, progress_callback, log_callback, cancel_event)

            output_name = safe_filename(task.output_name, "video.mp4") if task.output_name else "video.mp4"
            if not Path(output_name).suffix:
                output_name = f"{output_name}.mp4"
            output_path = unique_path(task.output_dir / output_name)
            combine_with_concat_demuxer(files, output_path, log_callback)
            progress_callback(1.0, "HLS 下载完成")
            return DownloadResult(True, output_path, f"已保存到 {output_path}")
        except Exception as exc:
            return DownloadResult(False, None, "HLS 下载失败", [str(exc)])

    def _download_segments(
        self,
        segments: list[str],
        segment_dir: Path,
        task: DownloadTask,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
        cancel_event: Event,
    ) -> list[Path]:
        segment_dir.mkdir(parents=True, exist_ok=True)
        completed = 0
        files: list[Path | None] = [None] * len(segments)

        def fetch(index: int, url: str) -> tuple[int, Path]:
            if cancel_event.is_set():
                raise RuntimeError("下载已取消")
            suffix = Path(urlparse(url).path).suffix or ".bin"
            name = f"segment-{index:05d}{suffix}"
            with requests.Session() as worker_session:
                return index, download_file(worker_session, url, segment_dir / name, task, log_callback, cancel_event)

        workers = max(1, min(task.concurrency, 16))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch, index, url) for index, url in enumerate(segments)]
            for future in as_completed(futures):
                if cancel_event.is_set():
                    raise RuntimeError("下载已取消")
                index, path = future.result()
                files[index] = path
                completed += 1
                progress_callback(completed / max(len(segments), 1) * 0.9, f"已下载 {completed}/{len(segments)}")

        return [path for path in files if path is not None]
