"""网页嗅探：页面源码扫描、主播放列表解析、清晰度选择，以及「网页 -> m3u8 -> 合并」端到端。"""
from __future__ import annotations

import functools
import http.server
import socketserver
import subprocess
import sys
import tempfile
import unittest
import threading
from collections.abc import Mapping
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_loader.downloaders.sniff import SniffDownloader  # noqa: E402
from video_loader.models import DownloadResult, DownloadTask, StreamVariant  # noqa: E402
from video_loader.services import sniffer  # noqa: E402
from video_loader.services.browser_capture import BrowserCapture  # noqa: E402
from video_loader.services.ffmpeg import find_ffmpeg  # noqa: E402
from video_loader.services.http_client import (  # noqa: E402
    build_session,
    curl_cffi_available,
    request_headers,
    transport_label,
)
from video_loader.services.sniffer import (  # noqa: E402
    describe_variants,
    find_m3u8_urls,
    parse_master_playlist,
    pick_variant,
    sort_variants,
)

MASTER_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=1456248,AVERAGE-BANDWIDTH=698738,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2"
360p/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2433096,AVERAGE-BANDWIDTH=1076756,RESOLUTION=854x480
480p/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=8073848,AVERAGE-BANDWIDTH=3884059,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
1080p/video.m3u8
"""

MEDIA_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXTINF:2.000000,
video0.ts
#EXTINF:2.000000,
video1.ts
#EXTINF:2.000000,
video2.ts
#EXT-X-ENDLIST
"""

CHALLENGE_PAGE = (
    "<html><head><title>Just a moment...</title></head><body>"
    '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
    "请稍候…</body></html>"
)


class _LocalServer:
    """起一个只读静态服务器，供本地页面 / 播放列表测试使用。"""

    def __init__(self, root: Path) -> None:
        handler = functools.partial(_QuietHandler, directory=str(root))
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_LocalServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 覆盖基类签名
        pass


def _write_site(root: Path) -> None:
    (root / "hls").mkdir(parents=True, exist_ok=True)
    (root / "hls" / "master.m3u8").write_text(MASTER_PLAYLIST, encoding="utf-8")
    for quality in ("360p", "480p", "1080p"):
        folder = root / "hls" / quality
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "video.m3u8").write_text(MEDIA_PLAYLIST, encoding="utf-8")
    (root / "page.html").write_text(
        "<html><head><title>示例视频页</title></head><body>"
        "<h1>demo</h1>"
        '<script>var player = { source: "hls\\/master.m3u8" };</script>'
        "</body></html>",
        encoding="utf-8",
    )
    (root / "frame.html").write_text('<script src="hls/master.m3u8"></script>', encoding="utf-8")
    (root / "page_iframe.html").write_text('<iframe src="frame.html"></iframe>', encoding="utf-8")
    (root / "empty.html").write_text("<html><head><title>无视频</title></head><body>nothing</body></html>", encoding="utf-8")
    (root / "challenge.html").write_text(CHALLENGE_PAGE, encoding="utf-8")


def _task(url: str, output_dir: Path, **overrides: object) -> DownloadTask:
    task = DownloadTask(url=url, mode="sniff", output_dir=output_dir)
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


class FindM3u8UrlsTests(unittest.TestCase):
    def test_finds_escaped_absolute_url(self) -> None:
        html = '<script>src = "https:\\/\\/cdn.example.com\\/a\\/playlist.m3u8";</script>'
        self.assertEqual(
            find_m3u8_urls(html, "https://site.example.com/watch/1"),
            ["https://cdn.example.com/a/playlist.m3u8"],
        )

    def test_resolves_relative_url_and_keeps_single_quotes(self) -> None:
        html = "<a href='hls/master.m3u8'>play</a>"
        self.assertEqual(find_m3u8_urls(html, "https://site.example.com/watch/1"), ["https://site.example.com/watch/hls/master.m3u8"])

    def test_dedupes_and_ignores_other_media(self) -> None:
        html = "a.mp4 b.ts c.m3u8 c.m3u8"
        self.assertEqual(find_m3u8_urls(html, "https://site.example.com/"), ["https://site.example.com/c.m3u8"])

    def test_empty_input(self) -> None:
        self.assertEqual(find_m3u8_urls("", "https://site.example.com/"), [])


class MasterPlaylistTests(unittest.TestCase):
    def test_parses_every_rendition(self) -> None:
        variants = parse_master_playlist(MASTER_PLAYLIST, "https://cdn.example.com/a/master.m3u8")
        self.assertEqual([variant.label for variant in variants], ["360p", "480p", "1080p"])
        self.assertEqual(variants[2].resolution, "1920x1080")
        self.assertEqual(variants[2].bandwidth, 8073848)
        self.assertEqual(variants[0].url, "https://cdn.example.com/a/360p/video.m3u8")
        self.assertEqual(variants[1].codecs, "")

    def test_media_playlist_has_no_variants(self) -> None:
        self.assertEqual(parse_master_playlist(MEDIA_PLAYLIST, "https://cdn.example.com/a/video.m3u8"), [])

    def test_sort_is_best_first(self) -> None:
        variants = sort_variants(parse_master_playlist(MASTER_PLAYLIST, "https://cdn.example.com/master.m3u8"))
        self.assertEqual([variant.quality_key for variant in variants], ["1080p", "480p", "360p"])

    def test_describe_includes_resolution_and_bandwidth(self) -> None:
        variant = StreamVariant(url="u", label="720p", resolution="1280x720", bandwidth=4572160)
        self.assertEqual(variant.describe(), "720p · 1280x720 · 4.6 Mbps")
        self.assertEqual(variant.height, 720)


class PickVariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.variants = parse_master_playlist(MASTER_PLAYLIST, "https://cdn.example.com/master.m3u8")

    def test_exact_label(self) -> None:
        self.assertEqual(pick_variant(self.variants, "480p").label, "480p")  # type: ignore[union-attr]

    def test_resolution_digits(self) -> None:
        self.assertEqual(pick_variant(self.variants, "1920").label, "1080p")  # type: ignore[union-attr]

    def test_unknown_label_falls_back_to_best(self) -> None:
        self.assertEqual(pick_variant(self.variants, "4320p").label, "1080p")  # type: ignore[union-attr]

    def test_empty_preference_picks_best(self) -> None:
        self.assertEqual(pick_variant(self.variants, "").label, "1080p")  # type: ignore[union-attr]

    def test_no_variants(self) -> None:
        self.assertIsNone(pick_variant([], "1080p"))


class DescribeVariantsTests(unittest.TestCase):
    def test_duplicate_labels_get_suffix(self) -> None:
        variants = [
            StreamVariant(url="a", label="默认"),
            StreamVariant(url="b", label="默认"),
        ]
        labels = [label for label, _variant in describe_variants(variants)]
        self.assertEqual(labels, ["默认", "默认 #2"])


class SniffPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_site(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finds_variants_from_page_source(self) -> None:
        logs: list[str] = []
        with _LocalServer(self.root) as server:
            task = _task(server.url("page.html"), self.root, use_browser=False)
            result = sniffer.sniff_page(task.url, task, logs.append)
            self.assertEqual(result.page_title, "示例视频页")
            self.assertEqual([variant.label for variant in result.variants], ["1080p", "480p", "360p"])
            self.assertTrue(all(variant.url.startswith(server.url("hls/")) for variant in result.variants))
            self.assertTrue(any("页面源码" in note or "浏览器" in note for note in result.notes + logs))

    def test_scans_one_level_of_iframes(self) -> None:
        with _LocalServer(self.root) as server:
            task = _task(server.url("page_iframe.html"), self.root, use_browser=False)
            result = sniffer.sniff_page(task.url, task, lambda _m: None)
            self.assertEqual([variant.label for variant in result.variants], ["1080p", "480p", "360p"])

    def test_challenge_page_without_browser_reports_clearly(self) -> None:
        with _LocalServer(self.root) as server:
            task = _task(server.url("challenge.html"), self.root, use_browser=False)
            with self.assertRaises(RuntimeError) as ctx:
                sniffer.sniff_page(task.url, task, lambda _m: None)
            self.assertIn("浏览器抓包", str(ctx.exception))

    def test_falls_back_to_browser_capture(self) -> None:
        """页面源码里没有 m3u8 时，用（这里被打桩的）浏览器抓包结果。"""
        captured: list[str] = []
        original = sniffer.capture_media_urls

        def fake_capture(page_url: str, **kwargs: object):
            captured.append(page_url)
            return BrowserCapture(urls=[server.url("hls/master.m3u8")], title="抓包标题")

        with _LocalServer(self.root) as server:
            sniffer.capture_media_urls = fake_capture  # type: ignore[assignment]
            self.addCleanup(lambda: setattr(sniffer, "capture_media_urls", original))
            task = _task(server.url("empty.html"), self.root, use_browser=True)
            result = sniffer.sniff_page(task.url, task, lambda _m: None)
        self.assertEqual(captured, [task.url])
        self.assertEqual([variant.label for variant in result.variants], ["1080p", "480p", "360p"])
        self.assertTrue(any("浏览器抓包" in note for note in result.notes))


class _RecordingSession:
    """替换 build_session 用：按项目规则算出实际请求头，记录后再转发给真实会话。"""

    def __init__(self, task: DownloadTask, calls: list[tuple[str, dict[str, str]]]) -> None:
        self._task = task
        self._calls = calls
        self._inner = requests.Session()

    def request(self, method: str, url: str, **kwargs: object):
        sent = kwargs.get("headers")
        headers = request_headers(self._task, headers=sent if isinstance(sent, Mapping) else None)
        self._calls.append((url, headers))
        kwargs["headers"] = headers
        return self._inner.request(method, url, **kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        self._inner.close()


class RefererPropagationTests(unittest.TestCase):
    """CDN 防盗链：嗅探和下载都必须带上来源页面。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_site(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sniff_page_sends_referer_for_page_and_playlists(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []
        original_build = sniffer.build_session

        with _LocalServer(self.root) as server:
            sniffer.build_session = lambda task, log_callback=None: _RecordingSession(task, calls)  # type: ignore[assignment]
            self.addCleanup(lambda: setattr(sniffer, "build_session", original_build))
            task = _task(server.url("page.html"), self.root, use_browser=False)
            result = sniffer.sniff_page(task.url, task, lambda _m: None)

        self.assertEqual(result.referer, task.url)
        self.assertTrue(calls, "嗅探过程应该发出请求")
        page_calls = [headers for url, headers in calls if url.endswith("page.html")]
        playlist_calls = [headers for url, headers in calls if url.endswith("master.m3u8")]
        self.assertTrue(page_calls, calls)
        self.assertTrue(playlist_calls, "解析播放列表也要带 Referer")
        for headers in page_calls + playlist_calls:
            self.assertEqual(headers.get("Referer"), task.url, calls)

    def test_sniff_downloader_passes_referer_to_hls_task(self) -> None:
        hls = _RecordingHls()
        downloader = SniffDownloader(hls=hls)  # type: ignore[arg-type]
        with _LocalServer(self.root) as server:
            task = _task(server.url("page.html"), self.root, preferred_quality="480p", use_browser=False)
            result = downloader.download(task, lambda *_a: None, lambda _m: None, threading.Event())
        self.assertTrue(result.success)
        self.assertEqual(hls.calls[0].referer, task.url)

    def test_selected_stream_url_derives_referer_from_page(self) -> None:
        hls = _RecordingHls()
        downloader = SniffDownloader(hls=hls)  # type: ignore[arg-type]
        with _LocalServer(self.root) as server:
            task = _task(
                server.url("page.html"),
                self.root,
                selected_stream_url=server.url("hls/480p/video.m3u8"),
                preferred_quality="480p",
            )
            result = downloader.download(task, lambda *_a: None, lambda _m: None, threading.Event())
        self.assertTrue(result.success)
        self.assertEqual(hls.calls[0].referer, task.url)

    def test_user_referer_is_kept(self) -> None:
        hls = _RecordingHls()
        downloader = SniffDownloader(hls=hls)  # type: ignore[arg-type]
        with _LocalServer(self.root) as server:
            task = _task(
                server.url("page.html"),
                self.root,
                headers={"Referer": "https://mine.example/"},
                preferred_quality="480p",
                use_browser=False,
            )
            result = downloader.download(task, lambda *_a: None, lambda _m: None, threading.Event())
        self.assertTrue(result.success)
        self.assertEqual(hls.calls[0].referer, "https://mine.example/")


class HttpSessionTests(unittest.TestCase):
    def test_impersonation_can_be_switched_off(self) -> None:
        task = _task("https://example.com/", Path("."), impersonate=False)
        with build_session(task) as session:
            self.assertIn("requests", type(session.inner).__module__)  # type: ignore[attr-defined]

    def test_impersonation_uses_curl_cffi_when_available(self) -> None:
        if not curl_cffi_available():
            self.skipTest("未安装 curl_cffi")
        task = _task("https://example.com/", Path("."), impersonate=True)
        with build_session(task) as session:
            self.assertIn("curl_cffi", type(session.inner).__module__)  # type: ignore[attr-defined]

    def test_transport_label_mentions_backend(self) -> None:
        self.assertTrue(transport_label())


class _RecordingHls:
    """记录被调用的 URL，避免测试真的下载。"""

    def __init__(self) -> None:
        self.calls: list[DownloadTask] = []

    def download(self, task: DownloadTask, *_args: object) -> DownloadResult:
        self.calls.append(task)
        return DownloadResult(True, task.output_dir / "stub.mp4", "stub")


class SniffDownloaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_site(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_selected_stream_url_skips_resniff(self) -> None:
        hls = _RecordingHls()
        downloader = SniffDownloader(hls=hls)  # type: ignore[arg-type]
        with _LocalServer(self.root) as server:
            task = _task(
                server.url("missing-page.html"),
                self.root,
                selected_stream_url=server.url("hls/480p/video.m3u8"),
                preferred_quality="480p",
            )
            result = downloader.download(task, lambda *_a: None, lambda _m: None, threading.Event())
        self.assertTrue(result.success)
        self.assertEqual(len(hls.calls), 1)
        self.assertEqual(hls.calls[0].url, task.selected_stream_url)
        self.assertEqual(hls.calls[0].mode, "hls")

    def test_sniffs_then_honours_preferred_quality(self) -> None:
        hls = _RecordingHls()
        downloader = SniffDownloader(hls=hls)  # type: ignore[arg-type]
        logs: list[str] = []
        with _LocalServer(self.root) as server:
            task = _task(server.url("page.html"), self.root, preferred_quality="360p", use_browser=False)
            result = downloader.download(task, lambda *_a: None, logs.append, threading.Event())
        self.assertTrue(result.success)
        self.assertTrue(hls.calls[0].url.endswith("/hls/360p/video.m3u8"), hls.calls[0].url)
        self.assertTrue(any("已选择" in line for line in logs))

    def test_returns_failure_when_page_has_no_stream(self) -> None:
        downloader = SniffDownloader(hls=_RecordingHls())  # type: ignore[arg-type]
        with _LocalServer(self.root) as server:
            task = _task(server.url("challenge.html"), self.root, use_browser=False)
            result = downloader.download(task, lambda *_a: None, lambda _m: None, threading.Event())
        self.assertFalse(result.success)
        self.assertTrue(result.errors)


class EndToEndSniffDownloadTests(unittest.TestCase):
    """真实走一遍：网页 -> 主播放列表 -> 片段 -> ffmpeg 合并。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = find_ffmpeg()
        if not cls.ffmpeg:
            raise unittest.SkipTest("没有 ffmpeg，跳过端到端合并测试")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.site = cls.root / "site"
        cls.site.mkdir(parents=True, exist_ok=True)
        _write_site(cls.site)
        cls._generate_segments()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    @classmethod
    def _generate_segments(cls) -> None:
        target = cls.site / "hls" / "480p"
        command = [
            cls.ffmpeg, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
            "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
            "-hls_segment_filename", str(target / "video%d.ts"),
            str(target / "video.m3u8"),
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            # 有的 ffmpeg 构建没有 libx264（例如只有 GPL-free 编码器），退回 mpeg2video/mp2。
            fallback = [
                cls.ffmpeg, "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                "-t", "6", "-c:v", "mpeg2video", "-f", "mpegts",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
                "-hls_segment_filename", str(target / "video%d.ts"),
                str(target / "video.m3u8"),
            ]
            completed = subprocess.run(fallback, capture_output=True, text=True)
        if completed.returncode != 0:
            raise unittest.SkipTest(f"无法生成测试片段：{completed.stderr.strip()[:200]}")

    def test_page_to_mp4(self) -> None:
        output_dir = self.root / "out"
        output_dir.mkdir(exist_ok=True)
        logs: list[str] = []
        with _LocalServer(self.site) as server:
            task = _task(
                server.url("page.html"),
                output_dir,
                output_name="e2e.mp4",
                preferred_quality="480p",
                concurrency=2,
                use_browser=False,
            )
            result = SniffDownloader().download(task, lambda *_a: None, logs.append, threading.Event())

        self.assertTrue(result.success, result.errors)
        self.assertIsNotNone(result.output_path)
        assert result.output_path is not None
        self.assertTrue(result.output_path.is_file())
        self.assertGreater(result.output_path.stat().st_size, 1000)
        self.assertTrue(any("已选择：480p" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
