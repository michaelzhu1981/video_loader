"""统一的 HTTP 会话构造：默认用 curl_cffi 伪装浏览器 TLS/HTTP2 指纹。

Cloudflare 之类的前置防护按指纹拦截（curl/requests 即使带完整请求头和 cf_clearance
cookie 也会 403），curl_cffi 复用真实浏览器的指纹才能通过。没有安装 curl_cffi 时
退回 requests，功能不变，但受指纹保护的站点会失败。

本文件同时兜住两个实测出来的坑：

1. **UA 必须和指纹一致**：自定义一个占位 User-Agent（例如 `Mozilla/5.0`）会覆盖 curl_cffi
   内置的 Chrome UA，Cloudflare 立刻 403（页面和 CDN 都拦）。指纹伪装时占位 UA 会被丢掉。
2. **Referer 常常是必填的**：同样的 UA/指纹，缺 Referer 也 403（CDN 尤其严格）。
   嗅探模式会自动把页面地址作为 Referer 传下去。
"""
from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

import requests

from video_loader.models import DownloadTask, HttpSession, LogCallback


DEFAULT_IMPERSONATE = "chrome"
CURL_CFFI_REQUIREMENT = "curl-cffi>=0.7,<1"

# 出现这些标记说明是"真浏览器"的完整 UA；只有占位 UA（Mozilla/5.0 之类）才会破坏指纹一致性。
REAL_USER_AGENT_TOKENS = ("chrome/", "chromium/", "firefox/", "safari/", "edg/", "edge/", "brave/", "opr/", "vivaldi/")


def curl_cffi_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False
    return True


def transport_label() -> str:
    """给界面状态栏用的一行说明。"""
    if curl_cffi_available():
        return f"curl_cffi（指纹伪装 {DEFAULT_IMPERSONATE}）"
    return "requests（未安装 curl_cffi，指纹伪装不可用）"


def looks_like_real_user_agent(value: str) -> bool:
    lowered = (value or "").lower()
    return any(token in lowered for token in REAL_USER_AGENT_TOKENS)


def first_header(headers: Mapping[str, str], name: str) -> str:
    """按大小写不敏感的方式取一个请求头（用户可能写 referer / REFERER）。"""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def drop_header(headers: dict[str, str], name: str) -> None:
    for key in [key for key in headers if key.lower() == name.lower()]:
        headers.pop(key, None)


def default_referer(page_url: str) -> str:
    """页面地址 -> 可用的 Referer（去掉 #fragment）。"""
    parts = urlsplit(page_url or "")
    if not parts.scheme or not parts.netloc:
        return ""
    return parts._replace(fragment="").geturl()


def sanitize_headers(headers: Mapping[str, str], impersonate: bool) -> tuple[dict[str, str], list[str]]:
    """返回 (实际要发送的请求头, 给用户看的提示)。

    指纹伪装时丢掉占位 User-Agent：它会覆盖 curl_cffi 的浏览器 UA，让指纹和 UA 对不上，
    Cloudflare 会直接 403（实测）。完整浏览器 UA 原样保留，尊重用户的自定义。
    """
    cleaned = dict(headers)
    notices: list[str] = []
    if not impersonate:
        return cleaned, notices

    user_agent = first_header(cleaned, "User-Agent")
    if user_agent and not looks_like_real_user_agent(user_agent):
        drop_header(cleaned, "User-Agent")
        notices.append(
            f"已忽略占位 User-Agent「{user_agent}」：它与指纹不一致会被 Cloudflare 直接 403，"
            "改用指纹内置的 Chrome UA。"
        )
    return cleaned, notices


def request_headers(task: DownloadTask, *, headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """按任务设置算出实际发送的请求头：清理会破坏指纹的 UA，并按需补上 Referer。"""
    cleaned, _notices = sanitize_headers(task.headers if headers is None else headers, task.impersonate)
    if task.referer and not first_header(cleaned, "Referer"):
        cleaned["Referer"] = task.referer
    return cleaned


class _SanitizingSession:
    """包一层会话：每次请求都过一遍请求头清理，调用方不用自己记得这件事。"""

    def __init__(self, session: HttpSession, task: DownloadTask) -> None:
        self._session = session
        self._task = task

    def request(self, method: str, url: str, **kwargs: object):
        sent = kwargs.get("headers")
        kwargs["headers"] = request_headers(self._task, headers=sent if isinstance(sent, Mapping) else self._task.headers)
        return self._session.request(method, url, **kwargs)  # type: ignore[arg-type]

    @property
    def inner(self) -> HttpSession:
        """被包装的真实会话（requests 或 curl_cffi），便于调试/测试断言。"""
        return self._session

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "_SanitizingSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def build_session(
    task: DownloadTask,
    log_callback: LogCallback | None = None,
    *,
    force_requests: bool = False,
) -> HttpSession:
    """按任务设置返回一个会话；调用方负责 close()，或用 with 语句。"""
    if task.impersonate and not force_requests:
        if curl_cffi_available():
            from curl_cffi import requests as curl_requests

            session: HttpSession = curl_requests.Session(impersonate=DEFAULT_IMPERSONATE)
            if log_callback:
                log_callback(f"已启用浏览器指纹伪装：curl_cffi / {DEFAULT_IMPERSONATE}。")
            return _SanitizingSession(session, task)
        if log_callback:
            log_callback(
                f"未安装 curl_cffi，本次使用 requests；Cloudflare 等按指纹拦截的站点会返回 403。"
                f"安装命令：pip install '{CURL_CFFI_REQUIREMENT}'"
            )
    return _SanitizingSession(requests.Session(), task)
