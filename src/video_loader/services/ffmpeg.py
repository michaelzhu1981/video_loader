from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable


FFMPEG_PIP_PACKAGE = "imageio-ffmpeg>=0.5,<1"


def _venv_bin_dir() -> Path:
    return Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")


def _venv_ffmpeg_path() -> Path:
    return _venv_bin_dir() / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")


def is_running_in_venv() -> bool:
    return sys.prefix != sys.base_prefix


def find_ffmpeg() -> str | None:
    venv_ffmpeg = find_venv_ffmpeg()
    if venv_ffmpeg:
        return venv_ffmpeg
    return shutil.which("ffmpeg") or find_imageio_ffmpeg()


def find_venv_ffmpeg() -> str | None:
    ffmpeg = _venv_ffmpeg_path()
    return str(ffmpeg) if ffmpeg.is_file() else None


def find_imageio_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    return str(ffmpeg) if ffmpeg.is_file() else None


def require_ffmpeg() -> str:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("PATH 中没有找到 ffmpeg。合并媒体前请先安装 ffmpeg。")
    return ffmpeg


def install_ffmpeg_to_venv(log_callback: Callable[[str], None] | None = None) -> str:
    if not is_running_in_venv():
        raise RuntimeError("当前程序不是从虚拟环境启动，无法安装 ffmpeg 到虚拟环境。请使用 ./run.sh 启动。")

    existing = find_venv_ffmpeg() or shutil.which("ffmpeg")
    if existing:
        return existing

    def log(message: str) -> None:
        if log_callback:
            log_callback(message)

    source = find_imageio_ffmpeg()
    if not source:
        log("正在安装 ffmpeg 到当前虚拟环境...")
        command = [sys.executable, "-m", "pip", "install", FFMPEG_PIP_PACKAGE]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert process.stdout is not None
        for line in process.stdout:
            log(line.rstrip())
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"ffmpeg 安装失败，pip 退出码：{code}")

        source = find_imageio_ffmpeg()
    if not source:
        raise RuntimeError("ffmpeg 安装后仍未找到可执行文件。")

    target = _venv_ffmpeg_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(0o755)
    log(f"ffmpeg 已安装到：{target}")
    return str(target)


def concat_list_path(output_path: Path) -> Path:
    """ffmpeg concat 解复用器要读的片段清单放在哪（和输出同目录，例如 video.concat.txt）。"""
    return output_path.with_suffix(".concat.txt")


def combine_with_concat_demuxer(files: list[Path], output_path: Path, log_callback) -> Path:
    if not files:
        raise ValueError("没有可合并的片段文件。")

    ffmpeg = require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = concat_list_path(output_path)
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
    try:
        return _run_ffmpeg(command, output_path, log_callback)
    finally:
        # 这份清单只在 ffmpeg 运行期间有用，而且里面记的正是合并后就要被删掉的片段路径：
        # 留着只会误导（照它去找文件已经找不到了），所以不管成败都不留。
        list_path.unlink(missing_ok=True)


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
