from __future__ import annotations

import atexit
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from typing import Callable

import requests

LogCallback = Callable[[str], None]


ARIA2_PIP_INSTALL_HINT = "brew install aria2"


def _venv_bin_dir() -> Path:
    return Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")


def _venv_aria2c_path() -> Path:
    name = "aria2c.exe" if sys.platform == "win32" else "aria2c"
    return _venv_bin_dir() / name


def find_aria2c() -> str | None:
    venv_aria2c = _venv_aria2c_path()
    if venv_aria2c.is_file():
        return str(venv_aria2c)
    return shutil.which("aria2c")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# 进程退出（关窗口、Ctrl-C）时统一收掉还在跑的 aria2c，避免残留后台进程
_LIVE_SESSIONS: set["Aria2Session"] = set()


@atexit.register
def _shutdown_live_sessions() -> None:
    for session in list(_LIVE_SESSIONS):
        try:
            session.stop()
        except Exception:
            pass


def install_aria2c_to_venv(log_callback: LogCallback | None = None) -> str:
    """macOS 下通过 Homebrew 安装 aria2 并复制到虚拟环境；其他平台无法自动安装。"""
    from video_loader.services.ffmpeg import is_running_in_venv

    if not is_running_in_venv():
        raise RuntimeError("当前程序不是从虚拟环境启动，无法把 aria2 安装到虚拟环境。请使用 ./run.sh 启动。")

    existing = find_aria2c()
    if existing:
        return existing

    if sys.platform != "darwin":
        raise RuntimeError("无法自动安装 aria2：当前系统不支持自动安装，请手动安装 aria2 并确保 aria2c 在 PATH 中。")

    brew = shutil.which("brew")
    if not brew:
        raise RuntimeError("未找到 Homebrew，无法自动安装 aria2。请手动执行 `brew install aria2` 后重试。")

    def log(message: str) -> None:
        if log_callback:
            log_callback(message)

    if not find_aria2c():
        log("正在通过 Homebrew 安装 aria2...")
        process = subprocess.Popen(
            [brew, "install", "aria2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log(line.rstrip())
        code = process.wait()
        if code != 0:
            raise RuntimeError(f"Homebrew 安装 aria2 失败，退出码：{code}")

    source = find_aria2c()
    if not source:
        raise RuntimeError("Homebrew 安装后仍未找到 aria2c。")

    target = _venv_aria2c_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(0o755)
    log(f"aria2c 已安装到：{target}")
    return str(target)


class Aria2Session:
    """单个下载任务对应的临时 aria2c 进程 + JSON-RPC 客户端。"""

    def __init__(self, directory: Path, log_callback: LogCallback | None = None) -> None:
        self.directory = directory
        self.log_callback = log_callback
        self.secret = secrets.token_hex(16)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/jsonrpc"
        self.process: subprocess.Popen | None = None

    @property
    def aria2_path(self) -> str:
        aria2 = find_aria2c()
        if not aria2:
            raise RuntimeError(
                f"PATH 中没有找到 aria2c。请先安装 aria2（macOS：{ARIA2_PIP_INSTALL_HINT}）。"
            )
        return aria2

    def start(self) -> None:
        path = self.aria2_path
        command = [
            path,
            "--enable-rpc",
            f"--rpc-secret={self.secret}",
            f"--rpc-listen-port={self.port}",
            f"--dir={self.directory}",
            "--seed-time=0",
            "--quiet",
        ]
        if sys.platform != "win32":
            command.append("--log=/dev/null")
        # stdout/stderr 用 DEVNULL：管道没人读取时写满缓冲区会让 aria2c 卡死
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _LIVE_SESSIONS.add(self)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                code = self.process.returncode
                self.process = None
                _LIVE_SESSIONS.discard(self)
                raise RuntimeError(f"aria2c 进程启动后立即退出（退出码 {code}），请检查 aria2 版本（需要 1.18 及以上）。")
            try:
                self.call("aria2.getVersion", [])
                return
            except Exception:
                time.sleep(0.1)
        # RPC 一直没起来，必须先结束进程再报错，否则会残留一个后台 aria2c
        self.stop()
        raise RuntimeError("aria2c RPC 服务启动超时。")

    def call(self, method: str, params: list) -> dict:
        # aria2 的 JSON-RPC 接口要求 secret 放在 params 首位；新版（1.37+）要求 "token:" 前缀
        result = self._post([f"token:{self.secret}"] + params, method)
        if result is None:  # 旧版 aria2 只接受裸 secret，降级重试
            result = self._post([self.secret] + params, method)
        if result is None:
            raise RuntimeError("aria2 RPC 认证失败（Unauthorized）。请检查 aria2 版本。")
        return result

    def _post(self, params: list, method: str) -> dict | None:
        payload = {"jsonrpc": "2.0", "id": "video-loader", "method": method, "params": params}
        try:
            response = requests.post(self.url, json=payload, timeout=10)
        except requests.RequestException as exc:
            raise RuntimeError(f"无法连接 aria2 RPC（{self.url}）：{exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"aria2 RPC 返回了非 JSON 响应（HTTP {response.status_code}）。") from exc
        error = data.get("error")
        if error is None:
            return data.get("result", {})
        message = str(error.get("message", error))
        if "nauthorized" in message:
            return None
        raise RuntimeError(f"aria2 RPC 错误：{message}")

    def stop(self) -> None:
        _LIVE_SESSIONS.discard(self)
        if self.process is None:
            return
        try:
            self.call("aria2.shutdown", [])
        except Exception:
            pass
        try:
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()
            try:
                self.process.wait(timeout=5)
            except Exception:
                pass
        self.process = None


def _log(session: Aria2Session, message: str) -> None:
    callback = getattr(session, "log_callback", None)
    if callback:
        callback(message)


def _format_speed(raw: object) -> str:
    try:
        speed = float(str(raw or 0))
    except ValueError:
        return "0 KiB/s"
    if speed >= 1024 * 1024:
        return f"{speed / 1024 / 1024:.1f} MiB/s"
    return f"{speed / 1024:.0f} KiB/s"


def _seeders(status: dict) -> str:
    """aria2 的 tellStatus 把种籽数放在顶层 numSeeders（bittorrent 子字典里只有 announceList 等）。"""
    return str(status.get("numSeeders") or 0)


def wait_for_gid(
    session: Aria2Session,
    gid: str,
    cancel_event: Event | None = None,
    poll_interval: float = 1.0,
    timeout: float = 30 * 60,
    heartbeat_interval: float = 5.0,
) -> dict:
    """轮询 tellStatus 直到任务结束，返回最终状态字典。

    磁力链接有两个阶段：aria2 先只下载种子元信息（此时 GID 立即变成 complete），
    真正下载文件的 GID 由 PostDownloadHandler 新建并通过 ``followedBy`` 指向，
    因此这里必须跟随 ``followedBy`` 继续等待，否则会把"只拿到元信息"误判为下载完成。

    元信息阶段 totalLength 一直是 0，进度无法显示；为避免界面长时间毫无反馈，
    这里按 ``heartbeat_interval`` 输出一次心跳（连接数和种籽数）。
    """
    deadline = time.monotonic() + timeout
    last_progress = ""
    last_heartbeat = time.monotonic()
    missing_retries = 0
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("下载已取消")
        try:
            status = session.call("aria2.tellStatus", [gid])
        except RuntimeError as exc:
            # followedBy 指向的新 GID 可能晚几十到几百毫秒才注册，先重试几次再判定失败
            if "not found" in str(exc).lower() and missing_retries < 5:
                missing_retries += 1
                time.sleep(poll_interval)
                continue
            raise
        missing_retries = 0

        followed = status.get("followedBy") or []
        if followed:
            # 元信息阶段结束，跟随到实际下载任务
            next_gid = str(followed[0])
            _log(session, f"已获取种子元信息，开始下载文件（GID {next_gid[:8]}）")
            gid = next_gid
            last_progress = ""
            last_heartbeat = time.monotonic()
            continue

        state = str(status.get("status") or "")
        if state in ("complete", "error", "removed"):
            return status

        total = int(status.get("totalLength") or 0)
        done = int(status.get("completedLength") or 0)
        now = time.monotonic()
        if total > 0:
            percent = done * 100 / total
            snapshot = f"{percent:.1f}"
            if snapshot != last_progress:
                last_progress = snapshot
                last_heartbeat = now
                _log(session, f"进度 {snapshot}%  种籽 {_seeders(status)}  速度 {_format_speed(status.get('downloadSpeed'))}")
        elif now - last_heartbeat >= heartbeat_interval:
            last_heartbeat = now
            _log(
                session,
                f"等待种子元信息…（已连接 {status.get('connections') or 0} 个 peer，"
                f"种籽 {_seeders(status)}，速度 {_format_speed(status.get('downloadSpeed'))}）",
            )
        time.sleep(poll_interval)
    raise RuntimeError(f"aria2 下载超时（{timeout / 60:.0f} 分钟内没有完成）。")
