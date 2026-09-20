from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_loader.services import ffmpeg as ffmpeg_service
from video_loader.services.ffmpeg import (
    combine_with_concat_demuxer,
    concat_list_path,
    find_ffmpeg,
    install_ffmpeg_to_venv,
    require_ffmpeg,
)


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


class ConcatListCleanupTests(unittest.TestCase):
    """concat 清单只在 ffmpeg 运行期间存在：里面记的是合并后就被删掉的片段路径，留着只会误导。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.segments: list[Path] = []
        for index in range(2):
            path = self.root / f"_segments/segment-{index:05d}.ts"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 16)
            self.segments.append(path)
        self.output = self.root / "out" / "video.mp4"

    def _run(self, fake_run):
        with patch.object(ffmpeg_service, "require_ffmpeg", return_value="/usr/local/bin/ffmpeg"), patch.object(
            ffmpeg_service, "_run_ffmpeg", fake_run
        ):
            return combine_with_concat_demuxer(self.segments, self.output, lambda _m: None)

    @staticmethod
    def _list_from_command(command: list[str]) -> Path:
        return Path(command[command.index("-i") + 1])

    def test_concat_list_exists_during_merge_and_is_deleted_after(self) -> None:
        seen: list[Path] = []

        def fake_run(command, output_path, _log_callback):
            list_path = self._list_from_command(command)
            self.assertTrue(list_path.is_file(), "ffmpeg 运行时清单必须还在")
            self.assertEqual(
                list_path.read_text(encoding="utf-8"),
                "".join(f"file '{path}'\n" for path in self.segments),
                "清单内容必须是这次的片段列表",
            )
            seen.append(list_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"merged")
            return output_path

        result = self._run(fake_run)

        self.assertEqual(result, self.output)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], concat_list_path(self.output))
        self.assertFalse(seen[0].exists(), "合并成功不该留下 .concat.txt")
        self.assertEqual(sorted(path.name for path in self.output.parent.iterdir()), ["video.mp4"])

    def test_concat_list_is_deleted_even_when_ffmpeg_fails(self) -> None:
        def exploding_run(command, _output_path, _log_callback):
            self.assertTrue(self._list_from_command(command).is_file())
            raise RuntimeError("ffmpeg 执行失败，退出码：1")

        with self.assertRaises(RuntimeError):
            self._run(exploding_run)

        self.assertEqual(list(self.output.parent.glob("*.concat.txt")), [], "ffmpeg 失败也不该留下清单")


if __name__ == "__main__":
    unittest.main()
