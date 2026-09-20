"""用本机 Chrome + DevTools 协议抓取播放器真正请求的媒体地址。

页面被 Cloudflare 的 JS 挑战挡住时，纯 HTTP 请求拿不到任何东西（连 HTML 都只有
"Just a moment..."）。播放器在真实浏览器里能过挑战，所以这里直接开一个临时 profile
的 Chrome，监听 Network 域名下所有 .m3u8 请求。

注意：`--headless=new` 过不了 Cloudflare 挑战（标题会一直停在「请稍候…」），
因此默认使用有窗口的 Chrome；用临时 user-data-dir，不影响用户自己的浏览器配置。
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from video_loader.models import LogCallback


CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
MEDIA_SUFFIXES = (".m3u8", ".mpd")


@dataclass(slots=True)
class BrowserCapture:
    urls: list[str] = field(default_factory=list)
    title: str = ""
    notes: list[str] = field(default_factory=list)


def websocket_available() -> bool:
    try:
        import websocket  # noqa: F401
    except ImportError:
        return False
    return True


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")


def browser_available() -> bool:
    return websocket_available() and find_chrome() is not None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_cdp(port: int, process: subprocess.Popen, deadline: float) -> bool:
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
                json.load(response)
            return True
        except Exception:
            time.sleep(0.4)
    return False


def capture_media_urls(
    page_url: str,
    *,
    timeout: int = 45,
    headless: bool = False,
    chrome_path: str | None = None,
    log_callback: LogCallback | None = None,
) -> BrowserCapture:
    """打开 page_url 并返回播放器请求过的 m3u8/mpd 地址。"""
    result = BrowserCapture()

    def log(message: str) -> None:
        if log_callback:
            log_callback(message)

    if not websocket_available():
        raise RuntimeError("缺少 websocket-client，无法使用浏览器抓包。安装：pip install 'websocket-client>=1.7'")

    chrome = chrome_path or find_chrome()
    if not chrome:
        raise RuntimeError("没有找到 Chrome/Chromium，无法使用浏览器抓包。")

    import websocket  # websocket-client

    port = _free_port()
    profile_dir = Path(tempfile.mkdtemp(prefix="video-loader-chrome-"))
    arguments = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-features=Translate,OptimizationGuideModelDownloading",
    ]
    if headless:
        arguments.append("--headless=new")
    else:
        arguments.append("--window-size=1280,900")
    arguments.append("about:blank")

    log(f"启动浏览器抓包（{'无头' if headless else '有窗口'}模式）：{Path(chrome).name}")
    process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = None
    try:
        if not _wait_for_cdp(port, process, deadline=time.time() + 30):
            raise RuntimeError("浏览器没有在 30 秒内启动 DevTools 接口。")

        request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(request, timeout=10) as response:
            target = json.load(response)

        client = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=5, suppress_origin=True, max_size=32 * 1024 * 1024
        )
        message_id = [0]

        def send(method: str, **params: object) -> None:
            message_id[0] += 1
            client.send(json.dumps({"id": message_id[0], "method": method, "params": params}))

        send("Network.enable")
        send("Page.enable")
        send("Page.navigate", url=page_url)

        seen: list[str] = []
        title = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = client.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            method = message.get("method")
            if method == "Network.requestWillBeSent":
                url = message["params"]["request"]["url"]
                if url.split("?")[0].lower().endswith(MEDIA_SUFFIXES) and url not in seen:
                    seen.append(url)
                    log(f"抓包命中：{url}")
            elif method == "Page.loadEventFired" and seen:
                break
            if seen and len(seen) >= 1:
                # 播放器通常只请求一个主播放列表，稍等一下让其它清晰度也注册进来。
                time.sleep(2)
                break

        result.urls = seen
        result.title = title
        if not seen:
            result.notes.append("浏览器抓包没有等到 m3u8 请求（页面可能没有自动播放，或需要点击播放按钮）。")
        return result
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)
