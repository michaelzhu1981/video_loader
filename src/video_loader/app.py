from __future__ import annotations

import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover - shown only when dependencies are missing.
    raise SystemExit("缺少依赖：请先运行 `pip install -r requirements.txt`。") from exc

from video_loader.downloaders.manager import DownloadManager, available_modes
from video_loader.models import DownloadResult, DownloadTask
from video_loader.services.ffmpeg import find_ffmpeg
from video_loader.utils import parse_cookies, parse_headers


MODE_LABELS = available_modes()
LABEL_TO_MODE = {label: mode for mode, label in MODE_LABELS.items()}


class VideoLoaderApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("视频下载器")
        self.geometry("1120x900")
        self.minsize(980, 720)

        self.manager = DownloadManager()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        self.mode_var = ctk.StringVar(value=MODE_LABELS["hls"])
        self.output_dir_var = ctk.StringVar(value=str(Path.cwd() / "downloads"))
        self.output_name_var = ctk.StringVar(value="video.mp4")
        self.concurrency_var = ctk.IntVar(value=4)
        self.timeout_var = ctk.IntVar(value=30)
        self.retries_var = ctk.IntVar(value=2)
        self.verify_ssl_var = ctk.BooleanVar(value=True)
        self.combine_var = ctk.BooleanVar(value=True)
        self.status_var = ctk.StringVar(value=self._ffmpeg_status())

        self._configure_grid()
        self._build_header()
        self._build_config_panel()
        self._build_log_panel()
        self.after(100, self._poll_events)

    def _configure_grid(self) -> None:
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="#0b0f14", corner_radius=0)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(header, text="视频下载器", font=ctk.CTkFont(size=22, weight="bold"))
        title.grid(row=0, column=0, sticky="w", padx=22, pady=(16, 2))

        subtitle = ctk.CTkLabel(
            header,
            textvariable=self.status_var,
            text_color="#8b949e",
            font=ctk.CTkFont(size=13),
        )
        subtitle.grid(row=1, column=0, sticky="w", padx=22, pady=(0, 16))

    def _build_config_panel(self) -> None:
        panel = ctk.CTkFrame(self, fg_color="#111820", corner_radius=10)
        panel.grid(row=1, column=0, sticky="nsew", padx=(18, 10), pady=18)
        panel.grid_columnconfigure(0, weight=1)

        section = ctk.CTkLabel(panel, text="下载任务", font=ctk.CTkFont(size=16, weight="bold"))
        section.grid(row=0, column=0, sticky="w", padx=18, pady=(18, 8))

        self.mode_menu = ctk.CTkOptionMenu(panel, values=list(LABEL_TO_MODE.keys()), variable=self.mode_var)
        self.mode_menu.grid(row=1, column=0, sticky="ew", padx=18, pady=6)

        self.url_box = ctk.CTkTextbox(panel, height=120, fg_color="#0d1117", border_width=1, border_color="#30363d")
        self.url_box.grid(row=2, column=0, sticky="ew", padx=18, pady=6)
        self.url_box.insert("1.0", "https://example.com/video.m3u8")

        dir_row = ctk.CTkFrame(panel, fg_color="transparent")
        dir_row.grid(row=3, column=0, sticky="ew", padx=18, pady=6)
        dir_row.grid_columnconfigure(0, weight=1)
        self.output_dir_entry = ctk.CTkEntry(dir_row, textvariable=self.output_dir_var, placeholder_text="保存目录")
        self.output_dir_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        browse = ctk.CTkButton(dir_row, text="选择", width=84, command=self._choose_output_dir)
        browse.grid(row=0, column=1)

        self.output_name_entry = ctk.CTkEntry(panel, textvariable=self.output_name_var, placeholder_text="输出文件名")
        self.output_name_entry.grid(row=4, column=0, sticky="ew", padx=18, pady=6)

        controls = ctk.CTkFrame(panel, fg_color="#0d1117", corner_radius=8)
        controls.grid(row=5, column=0, sticky="ew", padx=18, pady=10)
        controls.grid_columnconfigure((0, 1, 2), weight=1)
        self._number_field(controls, "并发数", self.concurrency_var, 0)
        self._number_field(controls, "超时秒数", self.timeout_var, 1)
        self._number_field(controls, "重试次数", self.retries_var, 2)

        self.verify_ssl = ctk.CTkCheckBox(panel, text="校验 SSL 证书", variable=self.verify_ssl_var)
        self.verify_ssl.grid(row=6, column=0, sticky="w", padx=18, pady=(6, 2))
        self.combine = ctk.CTkCheckBox(panel, text="合并已下载片段", variable=self.combine_var)
        self.combine.grid(row=7, column=0, sticky="w", padx=18, pady=(2, 10))

        advanced = ctk.CTkLabel(panel, text="请求头和 Cookie", font=ctk.CTkFont(size=14, weight="bold"))
        advanced.grid(row=8, column=0, sticky="w", padx=18, pady=(8, 6))
        self.headers_box = ctk.CTkTextbox(panel, height=86, fg_color="#0d1117", border_width=1, border_color="#30363d")
        self.headers_box.grid(row=9, column=0, sticky="ew", padx=18, pady=6)
        self.headers_box.insert("1.0", "User-Agent: Mozilla/5.0")
        self.cookies_box = ctk.CTkTextbox(panel, height=70, fg_color="#0d1117", border_width=1, border_color="#30363d")
        self.cookies_box.grid(row=10, column=0, sticky="ew", padx=18, pady=6)

        action_row = ctk.CTkFrame(panel, fg_color="transparent")
        action_row.grid(row=11, column=0, sticky="ew", padx=18, pady=(14, 18))
        action_row.grid_columnconfigure((0, 1), weight=1)
        self.start_button = ctk.CTkButton(action_row, text="开始", command=self._start_download, fg_color="#238636")
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.cancel_button = ctk.CTkButton(action_row, text="取消", command=self._cancel_download, state="disabled", fg_color="#8b3434")
        self.cancel_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _number_field(self, parent: ctk.CTkFrame, label: str, variable: ctk.IntVar, column: int) -> None:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=0, column=column, sticky="ew", padx=8, pady=10)
        ctk.CTkLabel(frame, text=label, text_color="#8b949e").grid(row=0, column=0, sticky="w")
        entry = ctk.CTkEntry(frame, textvariable=variable, width=76)
        entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))

    def _build_log_panel(self) -> None:
        panel = ctk.CTkFrame(self, fg_color="#0d1117", corner_radius=10)
        panel.grid(row=1, column=1, sticky="nsew", padx=(10, 18), pady=18)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(panel, text="运行日志", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=18, pady=(18, 6)
        )
        self.progress = ctk.CTkProgressBar(panel, height=12)
        self.progress.grid(row=1, column=0, sticky="ew", padx=18, pady=(4, 10))
        self.progress.set(0)

        self.log_box = ctk.CTkTextbox(panel, fg_color="#06090f", border_width=1, border_color="#30363d")
        self.log_box.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 18))
        self.log_box.insert("1.0", "准备就绪。\n")

    def _choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(Path.cwd()))
        if selected:
            self.output_dir_var.set(selected)

    def _start_download(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        try:
            task = self._build_task()
        except Exception as exc:
            messagebox.showerror("任务无效", str(exc))
            return

        self.cancel_event.clear()
        self.progress.set(0)
        self._set_running(True)
        self._log("开始下载...")
        self.worker = threading.Thread(target=self._run_download, args=(task,), daemon=True)
        self.worker.start()

    def _build_task(self) -> DownloadTask:
        url = self.url_box.get("1.0", "end").strip()
        if not url:
            raise ValueError("请输入 URL 或片段列表。")

        output_dir = Path(self.output_dir_var.get()).expanduser()
        if not output_dir:
            raise ValueError("请选择保存目录。")

        concurrency = max(1, int(self.concurrency_var.get()))
        timeout = max(1, int(self.timeout_var.get()))
        retries = max(0, int(self.retries_var.get()))

        return DownloadTask(
            url=url,
            mode=LABEL_TO_MODE[self.mode_var.get()],
            output_dir=output_dir,
            output_name=self.output_name_var.get().strip(),
            headers=parse_headers(self.headers_box.get("1.0", "end").strip()),
            cookies=parse_cookies(self.cookies_box.get("1.0", "end").strip()),
            concurrency=concurrency,
            timeout=timeout,
            retries=retries,
            verify_ssl=self.verify_ssl_var.get(),
            combine_segments=self.combine_var.get(),
        )

    def _run_download(self, task: DownloadTask) -> None:
        result = self.manager.download(task, self._queue_progress, self._queue_log, self.cancel_event)
        self.events.put(("result", result))

    def _cancel_download(self) -> None:
        self.cancel_event.set()
        self._log("正在取消...")

    def _queue_log(self, message: str) -> None:
        self.events.put(("log", message))

    def _queue_progress(self, value: float, message: str) -> None:
        self.events.put(("progress", (max(0.0, min(value, 1.0)), message)))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "progress":
                    value, message = payload  # type: ignore[misc]
                    self.progress.set(float(value))
                    if message:
                        self._log(str(message))
                elif kind == "result":
                    self._handle_result(payload)  # type: ignore[arg-type]
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _handle_result(self, result: DownloadResult) -> None:
        if result.success:
            self.progress.set(1)
            self._log(result.message)
        else:
            self._log(result.message)
            for error in result.errors:
                self._log(f"错误：{error}")
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _log(self, message: str) -> None:
        self.log_box.insert("end", f"{message}\n")
        self.log_box.see("end")

    def _ffmpeg_status(self) -> str:
        ffmpeg = find_ffmpeg()
        return f"通用桌面下载工具 - ffmpeg：{ffmpeg or '未找到'}"


def main() -> None:
    app = VideoLoaderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
