from __future__ import annotations

from .direct import DirectDownloader
from .hls import HlsDownloader
from .jpeg_sequence import JpegSequenceDownloader
from .magnet import MagnetDownloader
from .segment_list import SegmentListDownloader
from .sniff import SniffDownloader

__all__ = [
    "DirectDownloader",
    "HlsDownloader",
    "JpegSequenceDownloader",
    "MagnetDownloader",
    "SegmentListDownloader",
    "SniffDownloader",
]
