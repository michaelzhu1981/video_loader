from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_loader.services.ffmpeg import find_ffmpeg, require_ffmpeg


class FfmpegTests(unittest.TestCase):
    def test_find_ffmpeg_when_present(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/ffmpeg"):
            self.assertEqual(find_ffmpeg(), "/usr/local/bin/ffmpeg")

    def test_require_ffmpeg_when_missing(self) -> None:
        with patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                require_ffmpeg()


if __name__ == "__main__":
    unittest.main()
