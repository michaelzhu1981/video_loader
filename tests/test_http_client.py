"""请求头策略：占位 UA 会破坏指纹、Referer 是防盗链必备 —— 两条都在这里守住。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_loader.models import DownloadTask  # noqa: E402
from video_loader.services import http_client  # noqa: E402
from video_loader.services.http_client import (  # noqa: E402
    build_session,
    default_referer,
    first_header,
    looks_like_real_user_agent,
    request_headers,
    sanitize_headers,
)

PAGE_URL = "https://missav.ai/jur-786-uncensored-leak"
FULL_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def _task(**overrides: object) -> DownloadTask:
    task = DownloadTask(url=PAGE_URL, mode="sniff", output_dir=Path("."))
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


class UserAgentTests(unittest.TestCase):
    def test_placeholder_user_agents_are_detected(self) -> None:
        for value in ("Mozilla/5.0", "mozilla/5.0", "Mozilla/5.0 ", "curl/8.4.0", "python-requests/2.31"):
            with self.subTest(value=value):
                self.assertFalse(looks_like_real_user_agent(value))

    def test_full_browser_user_agents_are_kept(self) -> None:
        for value in (FULL_CHROME_UA, "Mozilla/5.0 (X11; Linux x86_64) Firefox/128.0", "Mozilla/5.0 (Macintosh) Safari/605.1.15"):
            with self.subTest(value=value):
                self.assertTrue(looks_like_real_user_agent(value))

    def test_placeholder_dropped_when_impersonating(self) -> None:
        cleaned, notices = sanitize_headers({"User-Agent": "Mozilla/5.0", "Accept": "*/*"}, impersonate=True)
        self.assertNotIn("User-Agent", cleaned)
        self.assertEqual(cleaned["Accept"], "*/*")
        self.assertEqual(len(notices), 1)
        self.assertIn("403", notices[0])

    def test_full_user_agent_kept_when_impersonating(self) -> None:
        cleaned, notices = sanitize_headers({"User-Agent": FULL_CHROME_UA}, impersonate=True)
        self.assertEqual(cleaned["User-Agent"], FULL_CHROME_UA)
        self.assertEqual(notices, [])

    def test_stub_user_agent_kept_when_not_impersonating(self) -> None:
        """没有指纹伪装时占位 UA 无害，用户想发就发。"""
        cleaned, notices = sanitize_headers({"User-Agent": "Mozilla/5.0"}, impersonate=False)
        self.assertEqual(cleaned["User-Agent"], "Mozilla/5.0")
        self.assertEqual(notices, [])

    def test_user_agent_case_insensitive(self) -> None:
        cleaned, _notices = sanitize_headers({"user-agent": "Mozilla/5.0"}, impersonate=True)
        self.assertEqual(cleaned, {})


class RefererTests(unittest.TestCase):
    def test_default_referer_strips_fragment(self) -> None:
        self.assertEqual(default_referer("https://site.example/watch/1#t=30"), "https://site.example/watch/1")

    def test_default_referer_ignores_non_urls(self) -> None:
        self.assertEqual(default_referer(""), "")
        self.assertEqual(default_referer("not a url"), "")

    def test_request_headers_injects_referer_from_task(self) -> None:
        task = _task(headers={"Accept": "*/*"}, referer=PAGE_URL)
        self.assertEqual(request_headers(task)["Referer"], PAGE_URL)

    def test_user_referer_wins_over_task_referer(self) -> None:
        task = _task(headers={"referer": "https://mine.example/"}, referer=PAGE_URL)
        sent = request_headers(task)
        self.assertEqual(first_header(sent, "Referer"), "https://mine.example/")
        self.assertEqual(len([key for key in sent if key.lower() == "referer"]), 1)

    def test_no_referer_injected_when_task_has_none(self) -> None:
        self.assertNotIn("Referer", request_headers(_task(headers={"Accept": "*/*"})))

    def test_first_header_is_case_insensitive(self) -> None:
        self.assertEqual(first_header({"REFERER": "x"}, "Referer"), "x")
        self.assertEqual(first_header({}, "Referer"), "")


class SessionWrapperTests(unittest.TestCase):
    """build_session 返回的会话要替所有下载器兜住请求头清理。"""

    class _RecordingSession:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        def request(self, method: str, url: str, **kwargs: object):
            sent = kwargs.get("headers")
            self.calls.append(dict(sent) if isinstance(sent, dict) else {})
            return object()

        def close(self) -> None:
            pass

    def test_every_request_is_sanitized(self) -> None:
        recorder = self._RecordingSession()
        original = http_client.requests.Session
        http_client.requests.Session = lambda: recorder  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(http_client.requests, "Session", original))
        task = _task(impersonate=True, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"}, referer=PAGE_URL)
        with build_session(task, force_requests=True) as session:
            session.request("GET", "https://cdn.example/a.m3u8", headers=task.headers)
        self.assertEqual(len(recorder.calls), 1)
        self.assertNotIn("User-Agent", recorder.calls[0])
        self.assertEqual(recorder.calls[0]["Referer"], PAGE_URL)

    def test_impersonation_off_keeps_user_agent_but_still_adds_referer(self) -> None:
        recorder = self._RecordingSession()
        original = http_client.requests.Session
        http_client.requests.Session = lambda: recorder  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(http_client.requests, "Session", original))
        task = _task(impersonate=False, headers={"User-Agent": "Mozilla/5.0"}, referer=PAGE_URL)
        with build_session(task, force_requests=True) as session:
            session.request("GET", "https://cdn.example/a.m3u8")
        self.assertEqual(recorder.calls[0]["User-Agent"], "Mozilla/5.0")
        self.assertEqual(recorder.calls[0]["Referer"], PAGE_URL)


if __name__ == "__main__":
    unittest.main()
