from __future__ import annotations

from .direct import DirectDownloader
from .hls import HlsDownloader
from .jpeg_sequence import JpegSequenceDownloader
from .segment_list import SegmentListDownloader

__all__ = [
    "DirectDownloader",
    "HlsDownloader",
    "JpegSequenceDownloader",
    "SegmentListDownloader",
]
