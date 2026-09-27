from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_loader.downloaders.hls import is_playlist_url, parse_m3u8_segments


class M3U8ParserTests(unittest.TestCase):
    def test_relative_segments(self) -> None:
        text = "#EXTM3U\n#EXTINF:4,\nseg-1.ts\nseg-2.ts"
        self.assertEqual(
            parse_m3u8_segments(text, "https://example.com/path/video.m3u8"),
            ["https://example.com/path/seg-1.ts", "https://example.com/path/seg-2.ts"],
        )

    def test_absolute_segments(self) -> None:
        text = "#EXTM3U\nhttps://cdn.example.com/a.ts"
        self.assertEqual(parse_m3u8_segments(text, "https://example.com/video.m3u8"), ["https://cdn.example.com/a.ts"])

    def test_jpeg_filter(self) -> None:
        text = "#EXTM3U\none.jpeg\ntwo.ts\nthree.jpg"
        self.assertEqual(
            parse_m3u8_segments(text, "https://example.com/video.m3u8", (".jpeg", ".jpg")),
            ["https://example.com/one.jpeg", "https://example.com/three.jpg"],
        )

    def test_empty_playlist(self) -> None:
        self.assertEqual(parse_m3u8_segments("#EXTM3U\n#EXT-X-ENDLIST", "https://example.com/video.m3u8"), [])

    def test_unsupported_hls_tags_fail_before_downloading_segments(self) -> None:
        for tag in (
            '#EXT-X-MAP:URI="init.mp4"',
            "#EXT-X-BYTERANGE:100@0",
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"',
            "#EXT-X-DISCONTINUITY",
            '#EXT-X-PART:DURATION=0.5,URI="part.ts"',
        ):
            with self.subTest(tag=tag), self.assertRaisesRegex(RuntimeError, "暂不支持"):
                parse_m3u8_segments(f"#EXTM3U\n{tag}\nsegment.ts", "https://example.com/video.m3u8")

    def test_explicitly_unencrypted_segments_still_parse(self) -> None:
        text = "#EXTM3U\n#EXT-X-KEY:METHOD=NONE\nsegment.ts"
        self.assertEqual(parse_m3u8_segments(text, "https://example.com/video.m3u8"), ["https://example.com/segment.ts"])

    def test_playlist_url_detection(self) -> None:
        self.assertTrue(is_playlist_url("https://example.com/high/video.m3u8"))
        self.assertFalse(is_playlist_url("https://example.com/high/segment.ts"))


if __name__ == "__main__":
    unittest.main()
