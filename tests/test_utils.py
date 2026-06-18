from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_loader.utils import filename_from_url, parse_cookies, parse_headers, safe_filename, unique_path


class UtilsTests(unittest.TestCase):
    def test_safe_filename_removes_invalid_characters(self) -> None:
        self.assertEqual(safe_filename('bad:/name?.mp4'), "bad__name_.mp4")

    def test_filename_from_url_decodes_name(self) -> None:
        self.assertEqual(filename_from_url("https://example.com/a%20b.mp4?token=1"), "a b.mp4")

    def test_unique_path_avoids_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mp4"
            path.write_text("existing", encoding="utf-8")
            self.assertEqual(unique_path(path).name, "video-1.mp4")

    def test_parse_headers(self) -> None:
        self.assertEqual(parse_headers("User-Agent: Test\nAccept: */*"), {"User-Agent": "Test", "Accept": "*/*"})

    def test_parse_cookies(self) -> None:
        self.assertEqual(parse_cookies("a=1; b=two"), {"a": "1", "b": "two"})


if __name__ == "__main__":
    unittest.main()
