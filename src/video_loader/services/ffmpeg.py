from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def require_ffmpeg() -> str:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("PATH 中没有找到 ffmpeg。合并媒体前请先安装 ffmpeg。")
    return ffmpeg


def combine_with_concat_demuxer(files: list[Path], output_path: Path, log_callback) -> Path:
    if not files:
        raise ValueError("没有可合并的片段文件。")

    ffmpeg = require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = output_path.with_suffix(".concat.txt")
    list_content = "\n".join(f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in files)
    list_path.write_text(list_content + "\n", encoding="utf-8")

    command = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        str(output_path),
    ]
    return _run_ffmpeg(command, output_path, log_callback)


def combine_with_concat_protocol(files: list[Path], output_path: Path, log_callback) -> Path:
    if not files:
        raise ValueError("没有可合并的片段文件。")

    ffmpeg = require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    joined = "|".join(str(path) for path in files)
    command = [ffmpeg, "-y", "-i", f"concat:{joined}", "-c", "copy", str(output_path)]
    return _run_ffmpeg(command, output_path, log_callback)


def _run_ffmpeg(command: list[str], output_path: Path, log_callback) -> Path:
    log_callback("正在运行 ffmpeg...")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None
    for line in process.stdout:
        log_callback(line.rstrip())
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg 执行失败，退出码：{code}")
    return output_path
