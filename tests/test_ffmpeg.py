from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_loader.services import ffmpeg as ffmpeg_service
from video_loader.services.ffmpeg import find_ffmpeg, install_ffmpeg_to_venv, require_ffmpeg


class FfmpegTests(unittest.TestCase):
    def test_find_ffmpeg_when_present(self) -> None:
        with patch.object(ffmpeg_service, "find_venv_ffmpeg", return_value=None), patch(
            "shutil.which", return_value="/usr/local/bin/ffmpeg"
        ):
            self.assertEqual(find_ffmpeg(), "/usr/local/bin/ffmpeg")

    def test_require_ffmpeg_when_missing(self) -> None:
        with patch.object(ffmpeg_service, "find_venv_ffmpeg", return_value=None), patch(
            "shutil.which", return_value=None
        ), patch.object(ffmpeg_service, "find_imageio_ffmpeg", return_value=None):
            with self.assertRaises(RuntimeError):
                require_ffmpeg()

    def test_find_ffmpeg_prefers_venv_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ffmpeg = Path(tmp) / "ffmpeg"
            ffmpeg.write_text("binary", encoding="utf-8")
            with patch.object(ffmpeg_service, "_venv_ffmpeg_path", return_value=ffmpeg), patch(
                "shutil.which", return_value="/usr/local/bin/ffmpeg"
            ):
                self.assertEqual(find_ffmpeg(), str(ffmpeg))

    def test_install_ffmpeg_rejects_non_venv(self) -> None:
        with patch.object(ffmpeg_service, "is_running_in_venv", return_value=False):
            with self.assertRaises(RuntimeError):
                install_ffmpeg_to_venv()

    def test_install_ffmpeg_copies_imageio_binary_to_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "package-ffmpeg"
            source.write_text("binary", encoding="utf-8")
            target = root / "bin" / "ffmpeg"

            with patch.object(ffmpeg_service, "is_running_in_venv", return_value=True), patch.object(
                ffmpeg_service, "find_venv_ffmpeg", return_value=None
            ), patch("shutil.which", return_value=None), patch.object(
                ffmpeg_service, "find_imageio_ffmpeg", return_value=str(source)
            ), patch.object(
                ffmpeg_service, "_venv_ffmpeg_path", return_value=target
            ):
                self.assertEqual(install_ffmpeg_to_venv(), str(target))
                self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
