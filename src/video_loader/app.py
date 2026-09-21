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
from video_loader.models import MAX_CONCURRENCY, DownloadResult, DownloadTask, SniffResult, StreamVariant, worker_count
from video_loader.services.app_log import (
    ignore_sighup,
    install_exception_logging,
    install_signal_logging,
    log_line,
    log_shutdown,
    log_startup,
)
from video_loader.services.aria2 import find_aria2c, install_aria2c_to_venv
from video_loader.services.browser_capture import browser_available
from video_loader.services.ffmpeg import find_ffmpeg, install_ffmpeg_to_venv
from video_loader.services.http_client import sanitize_headers, transport_label
from video_loader.services.sniffer import describe_variants, sniff_page
from video_loader.utils import parse_cookies, parse_headers


MODE_LABELS = available_modes()
LABEL_TO_MODE = {label: mode for mode, label in MODE_LABELS.items()}
MAGNET_LABEL = MODE_LABELS["magnet"]
SNIFF_LABEL = MODE_LABELS["sniff"]
MODE_DESCRIPTIONS = {
    "direct": "直链文件：下载单个文件 URL，适合 mp4、zip 等可直接访问的资源。",
    "hls": "HLS / m3u8：解析播放列表并下载媒体片段，完成后用 ffmpeg 合并成视频。",
    "segment_list": "片段列表：在输入框中按行粘贴多个片段 URL，可选择只保存片段或合并。",
    "jpeg_sequence": "JPEG 图片序列：下载播放列表中的 jpg/jpeg 图片片段，并合并为视频。",
    "magnet": "磁力链接：每行粘贴一个 magnet:? 链接，通过内置 aria2 下载 BitTorrent 资源。",
    "sniff": "网页嗅探：粘贴网页地址后自动识别 m3u8 视频地址，列出清晰度供选择，再直接下载。",
}
TASK_TOOLTIP = "\n".join(MODE_DESCRIPTIONS.values())
HEADER_PRESETS = {
    # 不写 User-Agent：指纹伪装会用 curl_cffi 内置的 Chrome UA，写占位 UA 反而会被 Cloudflare 403。
    "默认浏览器": "Accept: */*\nAccept-Language: zh-CN,zh;q=0.9,en;q=0.8",
    "Chrome 桌面": (
        "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36\n"
        "Accept: */*\n"
        "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8"
    ),
    "HLS / m3u8": "Accept: application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
    "带来源 Referer": "Referer: https://example.com/\nOrigin: https://example.com",
}
# 心跳：日志里长时间没有心跳又没有"退出"行，就说明进程是被信号杀掉的（不是自己退出）。
HEARTBEAT_SECONDS = 60
HEADER_HINT = (
    "请求头格式：每行一个。留空 User-Agent 时会用指纹伪装的 Chrome UA（推荐）；"
    "写 Mozilla/5.0 这类占位 UA 会被 Cloudflare 直接 403。\n"
    "Cloudflare 站点通常还需要 Referer；网页嗅探模式会自动用页面地址补上。"
)
COOKIE_HINT = "Cookie 格式：key=value; key2=value2，可直接粘贴浏览器 Network 面板里的 Cookie 值。"
AUTO_QUALITY_LABEL = "自动（最高清晰度）"
SNIFF_PLACEHOLDER = "https://example.com/watch/123456"
SNIFF_HINT = "网页嗅探：粘贴视频页面地址（不是 m3u8 链接），点「解析网页」列出清晰度后直接下载。"
PARSE_BUTTON_TEXT = "解析网页"
QUALITY_HINT = "清晰度：先点「解析网页」，再从解析结果里选择要下载的画质。"
WINDOW_WIDTH = 1120
WINDOW_MIN_WIDTH = 980
WINDOW_MIN_HEIGHT = 720
# 开局兜底高度：还没量出内容高度前先用它，免得先闪一个把底部按钮裁掉的矮窗口。
WINDOW_HEIGHT_FALLBACK = 1160
# 屏幕边距：窗口高度最多到「屏幕高度 - 这个值」，避免窗口比屏幕还高导致够不到按钮。
SCREEN_EDGE_MARGIN = 80


class VideoLoaderApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("视频下载器")
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT_FALLBACK}")
        self.minsize(WINDOW_MIN_WIDTH, WINDOW_HEIGHT_FALLBACK)

        self.manager = DownloadManager()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.task_tooltip: ctk.CTkToplevel | None = None

        self.mode_var = ctk.StringVar(value=MODE_LABELS["hls"])
        self.output_dir_var = ctk.StringVar(value=str(Path.cwd() / "downloads"))
        self.output_name_var = ctk.StringVar(value="video.mp4")
        # 数字输入框必须用 StringVar：customtkinter 的 Entry 回调会对变量取 int，
        # 用 IntVar 时用户一清空输入框就抛 _tkinter.TclError: expected floating-point number but got ""。
        self.concurrency_var = ctk.StringVar(value="4")
        self.timeout_var = ctk.StringVar(value="30")
        self.retries_var = ctk.StringVar(value="2")
        self.verify_ssl_var = ctk.BooleanVar(value=True)
        self.combine_var = ctk.BooleanVar(value=True)
        self.resume_var = ctk.BooleanVar(value=True)
        self.impersonate_var = ctk.BooleanVar(value=True)
        self.browser_var = ctk.BooleanVar(value=True)
        self.status_var = ctk.StringVar(value=self._ffmpeg_status())
        self.header_preset_var = ctk.StringVar(value="默认浏览器")
        self.quality_var = ctk.StringVar(value=AUTO_QUALITY_LABEL)
        self.quality_choices: list[tuple[str, StreamVariant]] = []
        self.parsing = False

        install_exception_logging(self, self._log)
        self.after(HEARTBEAT_SECONDS * 1000, self._heartbeat)
        self._configure_grid()
        self._build_header()
        self._build_config_panel()
        self._build_log_panel()
        self._fit_window_height()
        self.after(100, self._poll_events)

    def _fit_window_height(self) -> None:
        """按内容实际高度撑开窗口。

        左侧配置面板每行都是固定高度，窗口比内容矮时底部「开始 / 取消」会被裁掉，
        所以这里量一次内容高度再决定窗口高度（屏幕放不下时退让到屏幕可用高度）。
        """
        self.update_idletasks()
        limit = max(self.winfo_screenheight() - SCREEN_EDGE_MARGIN, WINDOW_MIN_HEIGHT)
        height = min(max(self.winfo_reqheight(), WINDOW_HEIGHT_FALLBACK), limit)
        self.geometry(f"{WINDOW_WIDTH}x{height}")
        self.minsize(WINDOW_MIN_WIDTH, height)

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

        self.mode_menu = ctk.CTkOptionMenu(
            panel,
            values=list(LABEL_TO_MODE.keys()),
            variable=self.mode_var,
            command=self._on_mode_change,
        )
        self.mode_menu.grid(row=1, column=0, sticky="ew", padx=18, pady=6)

        self.url_box = ctk.CTkTextbox(panel, height=100, fg_color="#0d1117", border_width=1, border_color="#30363d")
        self.url_box.grid(row=2, column=0, sticky="ew", padx=18, pady=6)
        self.url_box.insert("1.0", "https://example.com/video.m3u8")

        sniff_row = ctk.CTkFrame(panel, fg_color="transparent")
        sniff_row.grid(row=3, column=0, sticky="ew", padx=18, pady=6)
        sniff_row.grid_columnconfigure(1, weight=1)
        self.parse_button = ctk.CTkButton(
            sniff_row,
            text=PARSE_BUTTON_TEXT,
            width=104,
            command=self._parse_page,
            fg_color="#1f6feb",
        )
        self.parse_button.grid(row=0, column=0, sticky="w")
        self.quality_menu = ctk.CTkOptionMenu(
            sniff_row,
            values=[AUTO_QUALITY_LABEL],
            variable=self.quality_var,
            width=260,
            dynamic_resizing=False,
        )
        self.quality_menu.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ctk.CTkLabel(panel, text=QUALITY_HINT + "\\n" + SNIFF_HINT, text_color="#8b949e", wraplength=360, justify="left").grid(
            row=4, column=0, sticky="w", padx=18, pady=(0, 4)
        )

        dir_row = ctk.CTkFrame(panel, fg_color="transparent")
        dir_row.grid(row=5, column=0, sticky="ew", padx=18, pady=6)
        dir_row.grid_columnconfigure(0, weight=1)
        self.output_dir_entry = ctk.CTkEntry(dir_row, textvariable=self.output_dir_var, placeholder_text="保存目录")
        self.output_dir_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        browse = ctk.CTkButton(dir_row, text="选择", width=84, command=self._choose_output_dir)
        browse.grid(row=0, column=1)

        self.output_name_entry = ctk.CTkEntry(panel, textvariable=self.output_name_var, placeholder_text="输出文件名")
        self.output_name_entry.grid(row=6, column=0, sticky="ew", padx=18, pady=6)

        controls = ctk.CTkFrame(panel, fg_color="#0d1117", corner_radius=8)
        controls.grid(row=7, column=0, sticky="ew", padx=18, pady=10)
        controls.grid_columnconfigure((0, 1, 2), weight=1)
        self._number_field(controls, "并发数", self.concurrency_var, 0)
        self._number_field(controls, "超时秒数", self.timeout_var, 1)
        self._number_field(controls, "重试次数", self.retries_var, 2)

        self.verify_ssl = ctk.CTkCheckBox(panel, text="校验 SSL 证书", variable=self.verify_ssl_var)
        self.verify_ssl.grid(row=8, column=0, sticky="w", padx=18, pady=(6, 2))
        self.combine = ctk.CTkCheckBox(panel, text="合并已下载片段", variable=self.combine_var)
        self.combine.grid(row=9, column=0, sticky="w", padx=18, pady=(2, 2))
        self.impersonate = ctk.CTkCheckBox(
            panel,
            text="浏览器指纹伪装（curl_cffi，Cloudflare 站点必开）",
            variable=self.impersonate_var,
        )
        self.impersonate.grid(row=10, column=0, sticky="w", padx=18, pady=(2, 2))
        self.use_browser = ctk.CTkCheckBox(
            panel,
            text="源码里没有 m3u8 时启动浏览器抓包",
            variable=self.browser_var,
        )
        self.use_browser.grid(row=11, column=0, sticky="w", padx=18, pady=(2, 2))
        self.resume = ctk.CTkCheckBox(
            panel,
            text="断点续传（复用已下好的分片，半截分片用 Range 接着下）",
            variable=self.resume_var,
        )
        self.resume.grid(row=12, column=0, sticky="w", padx=18, pady=(2, 10))

        advanced_row = ctk.CTkFrame(panel, fg_color="transparent")
        advanced_row.grid(row=13, column=0, sticky="ew", padx=18, pady=(8, 6))
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
            row=14, column=0, sticky="w", padx=18, pady=(0, 4)
        )
        self.headers_box = ctk.CTkTextbox(panel, height=76, fg_color="#0d1117", border_width=1, border_color="#30363d")
        self.headers_box.grid(row=15, column=0, sticky="ew", padx=18, pady=6)
        self.headers_box.insert("1.0", HEADER_PRESETS[self.header_preset_var.get()])
        ctk.CTkLabel(panel, text=COOKIE_HINT, text_color="#8b949e", wraplength=360, justify="left").grid(
            row=16, column=0, sticky="w", padx=18, pady=(2, 4)
        )
        self.cookies_box = ctk.CTkTextbox(panel, height=60, fg_color="#0d1117", border_width=1, border_color="#30363d")
        self.cookies_box.grid(row=17, column=0, sticky="ew", padx=18, pady=6)

        action_row = ctk.CTkFrame(panel, fg_color="transparent")
        action_row.grid(row=18, column=0, sticky="ew", padx=18, pady=(14, 18))
        action_row.grid_columnconfigure((0, 1), weight=1)
        self.start_button = ctk.CTkButton(action_row, text="开始", command=self._start_download, fg_color="#238636")
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.cancel_button = ctk.CTkButton(action_row, text="取消", command=self._cancel_download, state="disabled", fg_color="#8b3434")
        self.cancel_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _number_field(self, parent: ctk.CTkFrame, label: str, variable: ctk.StringVar, column: int) -> None:
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

    def _on_mode_change(self, _selected: str) -> None:
        if self.mode_var.get() == SNIFF_LABEL:
            current = self.url_box.get("1.0", "end").strip()
            if not current:
                self.url_box.insert("1.0", SNIFF_PLACEHOLDER)
            elif ".m3u8" in current and "example.com" in current:
                self.url_box.delete("1.0", "end")
                self.url_box.insert("1.0", SNIFF_PLACEHOLDER)
            self._log(SNIFF_HINT)
        if self.mode_var.get() == MAGNET_LABEL:
            current = self.url_box.get("1.0", "end").strip()
            if not current or not current.startswith("magnet:"):
                self.url_box.delete("1.0", "end")
                # 以 # 开头的是注释行，下载时会忽略，避免误把提示文字当成链接提交
                self.url_box.insert(
                    "1.0",
                    "# 每行一个磁力链接，例如：\n"
                    "# magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567\n",
                )

    def _parse_page(self) -> None:
        """解析输入框里的网页，列出可选清晰度。"""
        if self.parsing or (self.worker and self.worker.is_alive()):
            return
        current_label = self.mode_var.get()
        if LABEL_TO_MODE.get(current_label) != "sniff":
            self.mode_var.set(SNIFF_LABEL)
            self._on_mode_change(SNIFF_LABEL)

        try:
            task = self._build_task()
        except Exception as exc:
            messagebox.showerror("无法解析", str(exc))
            return

        self.parsing = True
        self.parse_button.configure(state="disabled", text="解析中...")
        self._log(f"正在解析：{task.url}")
        threading.Thread(target=self._run_sniff, args=(task,), daemon=True).start()

    def _run_sniff(self, task: DownloadTask) -> None:
        try:
            result = sniff_page(task.url, task, self._queue_log)
        except Exception as exc:
            self.events.put(("sniff_error", str(exc)))
            return
        self.events.put(("sniff_ok", result))

    def _apply_sniff_result(self, result: SniffResult) -> None:
        self.quality_choices = describe_variants(result.variants)
        labels = [label for label, _variant in self.quality_choices]
        self.quality_menu.configure(values=labels or [AUTO_QUALITY_LABEL])
        self.quality_var.set(labels[0] if labels else AUTO_QUALITY_LABEL)

        if result.page_title:
            self._log(f"页面标题：{result.page_title}")
        for note in result.notes:
            self._log(f"提示：{note}")
        if not self.quality_choices:
            self._log("没有解析到可用的清晰度。")
            return

        self._log(f"共解析到 {len(self.quality_choices)} 个清晰度，默认已选最高画质：")
        for label, variant in self.quality_choices:
            self._log(f"  · {label}  {variant.url}")
        self._log("调整「清晰度」下拉框后点「开始」即可下载。")

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

        if task.mode == "magnet" and not find_aria2c():
            approved = messagebox.askyesno(
                "需要安装 aria2",
                "当前系统和虚拟环境中没有找到 aria2。\n\n"
                "磁力链接下载需要 aria2 提供 BitTorrent 支持（macOS 可通过 Homebrew 安装）。\n"
                "是否现在安装 aria2？",
            )
            if not approved:
                self._log("已取消：缺少 aria2，未开始磁力链接下载任务。")
                return
            self._start_install_aria2_then_download(task)
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
            self.events.put(("install_error", ("ffmpeg", str(exc))))
            return
        self.events.put(("ffmpeg_status", None))
        self._queue_log("ffmpeg 准备完成，开始下载...")
        self._run_download(task)

    def _start_install_aria2_then_download(self, task: DownloadTask) -> None:
        self.cancel_event.clear()
        self.progress.set(0)
        self._set_running(True)
        self._log("开始安装 aria2...")
        self.worker = threading.Thread(target=self._install_aria2_then_run_download, args=(task,), daemon=True)
        self.worker.start()

    def _install_aria2_then_run_download(self, task: DownloadTask) -> None:
        try:
            install_aria2c_to_venv(self._queue_log)
        except Exception as exc:
            self.events.put(("install_error", ("aria2", str(exc))))
            return
        self.events.put(("aria2_status", None))
        self._queue_log("aria2 准备完成，开始下载...")
        self._run_download(task)

    def _build_task(self) -> DownloadTask:
        url = self.url_box.get("1.0", "end").strip()
        if not url:
            raise ValueError("请输入 URL 或片段列表。")

        output_dir = Path(self.output_dir_var.get()).expanduser()
        if not output_dir:
            raise ValueError("请选择保存目录。")

        # 数字框改成 StringVar 后必须走 _number_field_value：留空/非法值都给明确提示，
        # 不再让 int("") 在按钮回调里抛 TclError/ValueError（界面表现为点了没反应）。
        requested_concurrency = self._number_field_value(self.concurrency_var.get(), "并发数", minimum=1, default=4)
        concurrency = worker_count(requested_concurrency)
        if concurrency != requested_concurrency:
            self._log(f"并发数上限为 {MAX_CONCURRENCY}，本次按 {concurrency} 处理。")
        timeout = self._number_field_value(self.timeout_var.get(), "超时秒数", minimum=1, default=30)
        retries = self._number_field_value(self.retries_var.get(), "重试次数", minimum=0, default=2)

        mode = LABEL_TO_MODE[self.mode_var.get()]
        if mode == "sniff":
            lines = [line.strip() for line in url.splitlines() if line.strip() and not line.strip().startswith("#")]
            if not lines:
                raise ValueError("网页嗅探模式请输入视频页面地址。")
            if lines[0].lower().endswith(".m3u8"):
                raise ValueError(
                    "网页嗅探模式请填写视频页面地址；如果已经有 m3u8 链接，请改用「HLS / m3u8」模式。"
                )
            url = lines[0]
            if len(lines) > 1:
                self._log("网页嗅探模式每次只处理第一个地址，其余行已忽略。")

        preferred_quality = ""
        selected_stream_url = ""
        if mode == "sniff":
            chosen = self.quality_var.get()
            for label, variant in self.quality_choices:
                if label == chosen:
                    preferred_quality = variant.quality_key or variant.label
                    selected_stream_url = variant.url
                    break

        headers, notices = sanitize_headers(
            parse_headers(self.headers_box.get("1.0", "end").strip()),
            self.impersonate_var.get(),
        )
        for notice in notices:
            self._log(notice)

        return DownloadTask(
            url=url,
            mode=mode,
            output_dir=output_dir,
            output_name=self.output_name_var.get().strip(),
            headers=headers,
            cookies=parse_cookies(self.cookies_box.get("1.0", "end").strip()),
            concurrency=concurrency,
            timeout=timeout,
            retries=retries,
            verify_ssl=self.verify_ssl_var.get(),
            combine_segments=self.combine_var.get(),
            resume=self.resume_var.get(),
            impersonate=self.impersonate_var.get(),
            use_browser=self.browser_var.get(),
            preferred_quality=preferred_quality,
            selected_stream_url=selected_stream_url,
        )

    def _run_download(self, task: DownloadTask) -> None:
        result = self.manager.download(task, self._queue_progress, self._queue_log, self.cancel_event)
        self.events.put(("result", result))

    @staticmethod
    def _number_field_value(text: str, label: str, *, minimum: int, default: int) -> int:
        """读取数字输入框：留空用默认值，填了非数字给明确提示。"""
        value = "" if text is None else str(text).strip()
        if not value:
            return default
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError(f"「{label}」需要填整数，当前是「{value}」。") from exc
        return max(minimum, number)

    def _task_needs_ffmpeg(self, task: DownloadTask) -> bool:
        return task.mode in {"hls", "jpeg_sequence", "sniff"} or (
            task.mode == "segment_list" and task.combine_segments
        )

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
                    tool, message = payload  # type: ignore[misc]
                    self._handle_install_error(str(tool), str(message))
                elif kind == "sniff_ok":
                    self.parsing = False
                    self._restore_parse_button()
                    self._apply_sniff_result(payload)  # type: ignore[arg-type]
                elif kind == "sniff_error":
                    self.parsing = False
                    self._restore_parse_button()
                    self._log(f"解析失败：{payload}")
                    messagebox.showerror("解析失败", str(payload))
                elif kind == "ffmpeg_status":
                    self.status_var.set(self._ffmpeg_status())
                elif kind == "aria2_status":
                    self.status_var.set(self._ffmpeg_status())
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _restore_parse_button(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.parse_button.configure(state="normal", text=PARSE_BUTTON_TEXT)

    def destroy(self) -> None:  # type: ignore[override]
        log_shutdown("窗口关闭（用户关闭或程序结束）")
        super().destroy()

    def _handle_result(self, result: DownloadResult) -> None:
        if result.success:
            self.progress.set(1)
            self._log(result.message)
        else:
            self._log(result.message)
            for error in result.errors:
                self._log(f"错误：{error}")
        self._set_running(False)

    def _handle_install_error(self, tool: str, message: str) -> None:
        self._log(f"{tool} 安装失败：{message}")
        self.status_var.set(self._ffmpeg_status())
        self._set_running(False)
        messagebox.showerror(f"{tool} 安装失败", message)

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        self.parse_button.configure(
            state="disabled" if running else "normal",
            text="解析中..." if running else PARSE_BUTTON_TEXT,
        )

    def _heartbeat(self) -> None:
        log_line("运行中（心跳）")
        self.after(HEARTBEAT_SECONDS * 1000, self._heartbeat)

    def _log(self, message: str) -> None:
        self.log_box.insert("end", f"{message}\n")
        self.log_box.see("end")

    def _ffmpeg_status(self) -> str:
        ffmpeg = find_ffmpeg()
        aria2 = find_aria2c()
        browser = "可用" if browser_available() else "不可用"
        return (
            f"通用桌面下载工具 - ffmpeg：{ffmpeg or '未找到'}  aria2：{aria2 or '未找到'}\n"
            f"HTTP：{transport_label()}  浏览器抓包：{browser}"
        )


def main() -> None:
    log_startup()
    install_signal_logging()
    app = VideoLoaderApp()
    # 放在 mainloop 之前：Tk 会自己接管 SIGHUP 并静默 exit(1)，必须在这里设成忽略
    ignore_sighup()
    try:
        app.mainloop()
    except BaseException as exc:  # 让崩溃也留下证据，而不是静默退出
        log_line(f"mainloop 异常退出：{exc!r}")
        raise
    finally:
        log_shutdown("mainloop 正常返回")


if __name__ == "__main__":
    main()
