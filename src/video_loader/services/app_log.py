"""应用级日志：把启动/退出/信号/异常写进一个固定文件。

目的是回答"程序为什么自己退了"：Tk 的回调异常默认只打到 stderr（从 Finder 或
终端启动时用户看不到），被信号杀掉更是完全无痕。有了这个文件，下次退出能直接看
到是关窗口、崩溃，还是收到 SIGHUP/SIGTERM。
"""
from __future__ import annotations

import os
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Callable


SIGNAL_NAMES = {getattr(signal, name): name for name in ("SIGHUP", "SIGTERM", "SIGINT") if hasattr(signal, name)}
LOG_ENV_VAR = "VIDEO_LOADER_LOG"


def log_path() -> Path:
    """日志文件位置：可用 VIDEO_LOADER_LOG 覆盖（测试用）。"""
    override = os.environ.get(LOG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "video_loader.log"
    return Path.home() / ".local" / "state" / "video_loader" / "video_loader.log"


def log_line(message: str) -> None:
    """追加一行日志；写日志失败绝不能影响主流程。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [{os.getpid()}] {message}\n")
    except Exception:
        pass


def install_exception_logging(window: object | None = None, ui_log: Callable[[str], None] | None = None) -> None:
    """接管 Tk 回调异常和未捕获异常，写进日志文件（并尽量显示到界面日志框）。"""

    def report(message: str) -> None:
        log_line(message)
        if ui_log:
            try:
                ui_log(message.splitlines()[0] if message else message)
            except Exception:
                pass

    def tk_report(exc_type, exc_value, exc_tb) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        report(f"Tk 回调异常：\n{detail}")

    def excepthook(exc_type, exc_value, exc_tb) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        report(f"未捕获异常：\n{detail}")

    if window is not None:
        try:
            window.report_callback_exception = tk_report  # type: ignore[attr-defined]
        except Exception:
            pass
    sys.excepthook = excepthook


def install_signal_logging() -> None:
    """给退出类信号装一个"记录证据再退出"的处理器。

    注意：在 Tk 的 mainloop 里这个处理器**不会被执行**（Tcl 的事件循环拿走了信号投递，
    实测心跳日志在跑但 handler 没触发，进程直接消失）。GUI 场景请靠 `heartbeat` 心跳 +
    `./run.sh --background` 的退出码记录来定位退出原因；此处理器只对非 Tk 代码路径有效。
    """

    def handler(signum: int, _frame: object) -> None:
        name = SIGNAL_NAMES.get(signum, str(signum))
        hint = "（终端/会话关闭，或父进程被杀）" if name == "SIGHUP" else ""
        log_line(f"收到信号 {name}{hint}，进程退出。")
        os._exit(128 + signum)

    for signum in SIGNAL_NAMES:
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass


def ignore_sighup() -> None:
    """让程序扛住"终端/会话关闭"。

    实测：Tk 的 mainloop 会自己接管 SIGHUP，直接静默 exit(1)——不留 traceback、不留崩溃
    报告、也不给 Python 的 signal 处理器机会，用户看到的就是"程序自己退了"。
    在 mainloop 之前把 SIGHUP 设成忽略，关终端就不会带走程序。
    """
    if not hasattr(signal, "SIGHUP"):
        return
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (ValueError, OSError):
        pass


def log_startup() -> None:
    log_line(f"启动：python={sys.executable} 日志={log_path()}")


def log_shutdown(reason: str) -> None:
    log_line(f"退出：{reason}")
