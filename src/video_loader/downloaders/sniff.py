from __future__ import annotations

from dataclasses import replace
from threading import Event

from video_loader.downloaders.hls import HlsDownloader
from video_loader.models import DownloadResult, DownloadTask, LogCallback, ProgressCallback
from video_loader.services.http_client import default_referer, first_header
from video_loader.services.sniffer import describe_variants, pick_variant, sniff_page


class SniffDownloader:
    """网页嗅探模式：先识别页面里的 m3u8 和清晰度，再交给 HLS 下载器。"""

    def __init__(self, hls: HlsDownloader | None = None) -> None:
        self._hls = hls or HlsDownloader()

    def download(
        self,
        task: DownloadTask,
        progress_callback: ProgressCallback,
        log_callback: LogCallback,
        cancel_event: Event,
    ) -> DownloadResult:
        try:
            if task.selected_stream_url:
                stream_url = task.selected_stream_url
                # 已解析过的流地址：Referer 沿用页面地址（CDN 做防盗链时缺它必 403）。
                referer = first_header(task.headers, "Referer") or default_referer(task.url)
                log_callback(f"使用已选择的清晰度：{task.preferred_quality or '默认'}")
                log_callback(f"流地址：{stream_url}")
                if referer and not first_header(task.headers, "Referer"):
                    log_callback(f"自动补上 Referer：{referer}")
            else:
                log_callback("正在识别网页中的 m3u8 视频地址...")
                sniffed = sniff_page(task.url, task, log_callback)
                for note in sniffed.notes:
                    log_callback(f"提示：{note}")
                if sniffed.page_title:
                    log_callback(f"页面标题：{sniffed.page_title}")

                variant = pick_variant(sniffed.variants, task.preferred_quality)
                if variant is None:
                    return DownloadResult(False, None, "网页嗅探失败：没有找到可用的视频流。", sniffed.notes)
                for label, item in describe_variants(sniffed.variants):
                    log_callback(f"可选清晰度：{label} -> {item.url}")
                log_callback(f"已选择：{variant.describe()}")
                stream_url = variant.url
                referer = sniffed.referer

            if cancel_event.is_set():
                raise RuntimeError("下载已取消")

            hls_task = replace(task, url=stream_url, mode="hls", referer=referer)
            return self._hls.download(hls_task, progress_callback, log_callback, cancel_event)
        except Exception as exc:
            return DownloadResult(False, None, "网页嗅探下载失败", [str(exc)])
