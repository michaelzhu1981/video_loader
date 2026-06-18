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
from video_loader.services.ffmpeg import find_ffmpeg, install_ffmpeg_to_venv
from video_loader.utils import parse_cookies, parse_headers


MODE_LABELS = available_modes()
LABEL_TO_MODE = {label: mode for mode, label in MODE_LABELS.items()}
MODE_DESCRIPTIONS = {
    "direct": "直链文件：下载单个文件 URL，适合 mp4、zip 等可直接访问的资源。",
    "hls": "HLS / m3u8：解析播放列表并下载媒体片段，完成后用 ffmpeg 合并成视频。",
    "segment_list": "片段列表：在输入框中按行粘贴多个片段 URL，可选择只保存片段或合并。",
    "jpeg_sequence": "JPEG 图片序列：下载播放列表中的 jpg/jpeg 图片片段，并合并为视频。",
}
TASK_TOOLTIP = "\n".join(MODE_DESCRIPTIONS.values())
HEADER_PRESETS = {
    "默认浏览器": "User-Agent: Mozilla/5.0\nAccept: */*",
    "Chrome 桌面": (
        "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36\n"
        "Accept: */*\n"
        "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8"
    ),
    "HLS / m3u8": "User-Agent: Mozilla/5.0\nAccept: application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
    "带来源 Referer": "User-Agent: Mozilla/5.0\nReferer: https://example.com/\nOrigin: https://example.com",
}
HEADER_HINT = "请求头格式：每行一个，例如 User-Agent: Mozilla/5.0、Referer: https://example.com/"
COOKIE_HINT = "Cookie 格式：key=value; key2=value2，可直接粘贴浏览器 Network 面板里的 Cookie 值。"


class VideoLoaderApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("视频下载器")
        self.geometry("1120x980")
        self.minsize(980, 720)

        self.manager = DownloadManager()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.task_tooltip: ctk.CTkToplevel | None = None

        self.mode_var = ctk.StringVar(value=MODE_LABELS["hls"])
        self.output_dir_var = ctk.StringVar(value=str(Path.cwd() / "downloads"))
        self.output_name_var = ctk.StringVar(value="video.mp4")
        self.concurrency_var = ctk.IntVar(value=4)
        self.timeout_var = ctk.IntVar(value=30)
        self.retries_var = ctk.IntVar(value=2)
        self.verify_ssl_var = ctk.BooleanVar(value=True)
        self.combine_var = ctk.BooleanVar(value=True)
        self.status_var = ctk.StringVar(value=self._ffmpeg_status())
        self.header_preset_var = ctk.StringVar(value="默认浏览器")

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

        section_row = ctk.CTkFrame(panel, fg_color="transparent")
        section_row.grid(row=0, column=0, sticky="w", padx=18, pady=(18, 8))
        section = ctk.CTkLabel(section_row, text="下载任务", font=ctk.CTkFont(size=16, weight="bold"))
        section.grid(row=0, column=0, sticky="w")
        help_icon = ctk.CTkLabel(
            section_row,
            text="?",
            width=22,
            height=22,
            corner_radius=11,
            fg_color="#30363d",
            text_color="#c9d1d9",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        help_icon.grid(row=0, column=1, sticky="w", padx=(8, 0))
        help_icon.bind("<Enter>", lambda _event: self._show_task_tooltip(help_icon))
        help_icon.bind("<Leave>", lambda _event: self._hide_task_tooltip())

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

        advanced_row = ctk.CTkFrame(panel, fg_color="transparent")
        advanced_row.grid(row=8, column=0, sticky="ew", padx=18, pady=(8, 6))
        advanced_row.grid_columnconfigure(0, weight=1)
        advanced = ctk.CTkLabel(advanced_row, text="请求头和 Cookie", font=ctk.CTkFont(size=14, weight="bold"))
        advanced.grid(row=0, column=0, sticky="w")
        self.header_preset_menu = ctk.CTkOptionMenu(
            advanced_row,
            values=list(HEADER_PRESETS.keys()),
            variable=self.header_preset_var,
            width=138,
            command=self._apply_header_preset,
        )
        self.header_preset_menu.grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(panel, text=HEADER_HINT, text_color="#8b949e", wraplength=360, justify="left").grid(
            row=9, column=0, sticky="w", padx=18, pady=(0, 4)
        )
        self.headers_box = ctk.CTkTextbox(panel, height=86, fg_color="#0d1117", border_width=1, border_color="#30363d")
        self.headers_box.grid(row=10, column=0, sticky="ew", padx=18, pady=6)
        self.headers_box.insert("1.0", HEADER_PRESETS[self.header_preset_var.get()])
        ctk.CTkLabel(panel, text=COOKIE_HINT, text_color="#8b949e", wraplength=360, justify="left").grid(
            row=11, column=0, sticky="w", padx=18, pady=(2, 4)
        )
        self.cookies_box = ctk.CTkTextbox(panel, height=70, fg_color="#0d1117", border_width=1, border_color="#30363d")
        self.cookies_box.grid(row=12, column=0, sticky="ew", padx=18, pady=6)

        action_row = ctk.CTkFrame(panel, fg_color="transparent")
        action_row.grid(row=13, column=0, sticky="ew", padx=18, pady=(14, 18))
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

    def _apply_header_preset(self, selected: str) -> None:
        preset = HEADER_PRESETS.get(selected)
        if not preset:
            return
        self.headers_box.delete("1.0", "end")
        self.headers_box.insert("1.0", preset)

    def _show_task_tooltip(self, anchor: ctk.CTkLabel) -> None:
        self._hide_task_tooltip()

        tooltip = ctk.CTkToplevel(self)
        tooltip.overrideredirect(True)
        tooltip.attributes("-topmost", True)

        frame = ctk.CTkFrame(tooltip, fg_color="#161b22", border_width=1, border_color="#30363d", corner_radius=8)
        frame.grid(row=0, column=0)
        ctk.CTkLabel(
            frame,
            text=TASK_TOOLTIP,
            text_color="#c9d1d9",
            justify="left",
            anchor="w",
            wraplength=360,
        ).grid(row=0, column=0, padx=12, pady=10)

        x = anchor.winfo_rootx() + anchor.winfo_width() + 8
        y = anchor.winfo_rooty() - 4
        tooltip.geometry(f"+{x}+{y}")
        self.task_tooltip = tooltip

    def _hide_task_tooltip(self) -> None:
        if self.task_tooltip is not None:
            self.task_tooltip.destroy()
            self.task_tooltip = None

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

        if self._task_needs_ffmpeg(task) and not find_ffmpeg():
            approved = messagebox.askyesno(
                "需要安装 ffmpeg",
                "当前系统和虚拟环境中没有找到 ffmpeg。\n\n"
                "HLS、片段列表合并和 JPEG 图片序列需要 ffmpeg 才能生成最终视频。\n"
                "是否安装 ffmpeg 到当前虚拟环境？",
            )
            if not approved:
                self._log("已取消：缺少 ffmpeg，未开始需要合并的下载任务。")
                return
            self._start_install_then_download(task)
            return

        self._start_worker(task)

    def _start_worker(self, task: DownloadTask) -> None:
        self.cancel_event.clear()
        self.progress.set(0)
        self._set_running(True)
        self._log("开始下载...")
        self.worker = threading.Thread(target=self._run_download, args=(task,), daemon=True)
        self.worker.start()

    def _start_install_then_download(self, task: DownloadTask) -> None:
        self.cancel_event.clear()
        self.progress.set(0)
        self._set_running(True)
        self._log("开始安装 ffmpeg...")
        self.worker = threading.Thread(target=self._install_then_run_download, args=(task,), daemon=True)
        self.worker.start()

    def _install_then_run_download(self, task: DownloadTask) -> None:
        try:
            install_ffmpeg_to_venv(self._queue_log)
        except Exception as exc:
            self.events.put(("install_error", str(exc)))
            return
        self.events.put(("ffmpeg_status", None))
        self._queue_log("ffmpeg 准备完成，开始下载...")
        self._run_download(task)

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

    def _task_needs_ffmpeg(self, task: DownloadTask) -> bool:
        return task.mode in {"hls", "jpeg_sequence"} or (task.mode == "segment_list" and task.combine_segments)

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
                elif kind == "install_error":
                    self._handle_install_error(str(payload))
                elif kind == "ffmpeg_status":
                    self.status_var.set(self._ffmpeg_status())
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

    def _handle_install_error(self, message: str) -> None:
        self._log(f"ffmpeg 安装失败：{message}")
        self.status_var.set(self._ffmpeg_status())
        self._set_running(False)
        messagebox.showerror("ffmpeg 安装失败", message)

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
