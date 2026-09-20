"""并发下载：分片真的并行、并发数被尊重、连接被复用，以及 aria2 拿到并发参数。

这些用例都用本地 HTTP 服务器在服务端计数（同时在处理的请求数、新开的 TCP 连接数），
不靠时序猜测：并行与否是服务端看到的事实。
"""
from __future__ import annotations

import http.server
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from threading import Event
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_loader.downloaders.base import download_many, download_one  # noqa: E402
from video_loader.downloaders.hls import HlsDownloader  # noqa: E402
from video_loader.downloaders.jpeg_sequence import JpegSequenceDownloader  # noqa: E402
from video_loader.downloaders.magnet import MagnetDownloader  # noqa: E402
from video_loader.downloaders.segment_list import SegmentListDownloader  # noqa: E402
from video_loader.models import MAX_CONCURRENCY, DownloadTask, worker_count  # noqa: E402
from video_loader.services import http_client  # noqa: E402
from video_loader.services.aria2 import Aria2Session  # noqa: E402

SEGMENT_PAYLOAD = b"segment-bytes" * 64
JPEG_PAYLOAD = b"\xff\xd8\xff\xe0jpeg-payload" * 32


def m3u8(paths: list[str]) -> bytes:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1"]
    for path in paths:
        lines.append("#EXTINF:1.000000,")
        lines.append(path)
    lines.append("#EXT-X-ENDLIST")
    return ("\n".join(lines) + "\n").encode("utf-8")


class _Slot:
    """占住一个"正在处理的请求"名额，用来测量服务端看到的并行度。"""

    def __init__(self, server: "_TrackingServer", path: str) -> None:
        self._server = server
        self._path = path

    def __enter__(self) -> "_Slot":
        with self._server.lock:
            self._server.active += 1
            self._server.max_active = max(self._server.max_active, self._server.active)
        time.sleep(self._server.delay_for(self._path))
        return self

    def __exit__(self, *_exc: object) -> None:
        with self._server.lock:
            self._server.active -= 1


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 允许长连接，才能测出"复用连接"这件事

    def do_GET(self) -> None:  # noqa: N802
        server: _TrackingServer = self.server  # type: ignore[assignment]
        path = self.path.lstrip("/")
        body = server.payloads.get(path)
        with _Slot(server, path):
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 覆盖基类签名
        pass


class _TrackingServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, payloads: dict[str, bytes], delay: float = 0.1, delays: dict[str, float] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.payloads = payloads
        self.delay = delay
        self.delays = delays or {}
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.connections = 0
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    def __enter__(self) -> "_TrackingServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        self.server_close()

    def delay_for(self, path: str) -> float:
        return self.delays.get(path, self.delay)

    def get_request(self):  # 每次新 TCP 连接记一笔
        conn, addr = super().get_request()
        with self.lock:
            self.connections += 1
        return conn, addr

    def url(self, path: str) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/{path.lstrip('/')}"


def fake_combine(files: list[Path], output_path: Path, _log: object = None) -> Path:
    """替掉 ffmpeg：只落一个占位文件，让下载逻辑本身成为被测对象。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"".join(Path(item).read_bytes() for item in files))
    return output_path


class WorkerCountTests(unittest.TestCase):
    def test_clamps_into_range(self) -> None:
        self.assertEqual(worker_count(0), 1)
        self.assertEqual(worker_count(-5), 1)
        self.assertEqual(worker_count(4), 4)
        self.assertEqual(worker_count(MAX_CONCURRENCY + 40), MAX_CONCURRENCY)
        self.assertEqual(worker_count("8"), 8)
        self.assertEqual(worker_count(None), 1)


class DownloadManyTests(unittest.TestCase):
    """download_many 是三个分片型下载器共用的实现，先把它的契约钉死。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _task(self, **overrides: object) -> DownloadTask:
        task = DownloadTask(url="http://127.0.0.1/unused", mode="segment_list", output_dir=self.root, **overrides)  # type: ignore[arg-type]
        return task

    def test_returns_paths_in_the_same_order_as_items(self) -> None:
        """合并要按顺序，所以返回顺序必须跟着 items，而不是完成顺序。"""
        payloads = {f"seg{i}.ts": SEGMENT_PAYLOAD for i in range(4)}
        delays = {"seg0.ts": 0.35, "seg1.ts": 0.05, "seg2.ts": 0.05, "seg3.ts": 0.05}
        with _TrackingServer(payloads, delay=0.05, delays=delays) as server:
            items = [(server.url(f"seg{i}.ts"), self.root / f"out{i}.ts") for i in range(4)]
            files = download_many(items, self._task(concurrency=4), lambda _m: None, Event())

        self.assertEqual([path.name for path in files], [f"out{i}.ts" for i in range(4)])
        for path in files:
            self.assertEqual(path.read_bytes(), SEGMENT_PAYLOAD)

    def test_cancel_event_stops_the_batch(self) -> None:
        payloads = {f"seg{i}.ts": SEGMENT_PAYLOAD for i in range(5)}
        cancel = Event()
        cancel.set()
        with _TrackingServer(payloads) as server:
            items = [(server.url(f"seg{i}.ts"), self.root / f"out{i}.ts") for i in range(5)]
            with self.assertRaises(RuntimeError) as ctx:
                download_many(items, self._task(concurrency=3), lambda _m: None, cancel)
        self.assertIn("取消", str(ctx.exception))
        self.assertEqual(list(self.root.glob("out*.ts")), [], "取消后不应留下半截文件")

    def test_single_item_reports_progress(self) -> None:
        payloads = {"only.ts": SEGMENT_PAYLOAD}
        updates: list[tuple[float, str]] = []
        with _TrackingServer(payloads) as server:
            items = [(server.url("only.ts"), self.root / "only.ts")]
            files = download_many(items, self._task(), lambda _m: None, Event(), lambda ratio, text: updates.append((ratio, text)))
        self.assertEqual(len(files), 1)
        self.assertTrue(updates, "单个文件也应回报进度")


class SegmentListConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, server: _TrackingServer, concurrency: int, count: int = 6) -> None:
        urls = "\n".join(server.url(f"seg{i}.ts") for i in range(count))
        task = DownloadTask(
            url=urls,
            mode="segment_list",
            output_dir=self.root / f"out{concurrency}",
            concurrency=concurrency,
            combine_segments=False,
        )
        result = SegmentListDownloader().download(task, lambda *_a: None, lambda _m: None, Event())
        self.assertTrue(result.success, result.errors)
        self.assertEqual(len(list((task.output_dir / "_segments").glob("segment-*.ts"))), count)

    def test_downloads_segments_in_parallel(self) -> None:
        payloads = {f"seg{i}.ts": SEGMENT_PAYLOAD for i in range(6)}
        with _TrackingServer(payloads, delay=0.15) as server:
            self._run(server, concurrency=4)
            self.assertGreaterEqual(server.max_active, 2, f"并发数=4 却只有 {server.max_active} 个请求同时在跑")

    def test_concurrency_one_stays_serial(self) -> None:
        payloads = {f"seg{i}.ts": SEGMENT_PAYLOAD for i in range(6)}
        with _TrackingServer(payloads, delay=0.05) as server:
            self._run(server, concurrency=1)
            self.assertEqual(server.max_active, 1, "并发数=1 时不应并行")

    def test_concurrency_is_clamped_to_max(self) -> None:
        payloads = {f"seg{i}.ts": SEGMENT_PAYLOAD for i in range(4)}
        with _TrackingServer(payloads, delay=0.15) as server:
            self._run(server, concurrency=MAX_CONCURRENCY + 40, count=4)
            self.assertLessEqual(server.max_active, MAX_CONCURRENCY)


class JpegSequenceConcurrencyTests(unittest.TestCase):
    def test_jpeg_sequence_downloads_in_parallel(self) -> None:
        payloads = {"list.m3u8": m3u8([f"pic{i}.jpeg" for i in range(6)])}
        payloads.update({f"pic{i}.jpeg": JPEG_PAYLOAD for i in range(6)})
        with tempfile.TemporaryDirectory() as tmp, _TrackingServer(payloads, delay=0.15) as server:
            out = Path(tmp) / "jpeg"
            task = DownloadTask(url=server.url("list.m3u8"), mode="jpeg_sequence", output_dir=out, concurrency=3)
            with mock.patch("video_loader.downloaders.jpeg_sequence.combine_with_concat_protocol", fake_combine):
                result = JpegSequenceDownloader().download(task, lambda *_a: None, lambda _m: None, Event())
            self.assertTrue(result.success, result.errors)
            self.assertGreaterEqual(server.max_active, 2, f"并发数=3 却只有 {server.max_active} 个请求同时在跑")
            self.assertEqual(sorted(path.name for path in out.glob("Video*")), [f"Video{i}.jpeg" for i in range(6)])


class HlsSessionReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _payloads(self, count: int) -> dict[str, bytes]:
        payloads = {"list.m3u8": m3u8([f"seg{i}.ts" for i in range(count)])}
        payloads.update({f"seg{i}.ts": SEGMENT_PAYLOAD for i in range(count)})
        return payloads

    def _run(self, server: _TrackingServer, concurrency: int, counters: dict[str, list]) -> None:
        real_build = http_client.build_session

        def counting_build(task, log_callback=None, **kwargs):
            session = real_build(task, log_callback, **kwargs)
            counters["sessions"].append(session)
            return session

        task = DownloadTask(url=server.url("list.m3u8"), mode="hls", output_dir=self.root / "hls", concurrency=concurrency)
        with mock.patch("video_loader.downloaders.base.build_session", counting_build), mock.patch(
            "video_loader.downloaders.hls.build_session", counting_build
        ), mock.patch("video_loader.downloaders.hls.combine_with_concat_demuxer", fake_combine):
            result = HlsDownloader().download(task, lambda *_a: None, lambda _m: None, Event())
        self.assertTrue(result.success, result.errors)
        counters["connections"].append(server.connections)

    def test_one_session_per_worker_not_per_segment(self) -> None:
        """每个分片新建会话会连指纹伪装/线程池都重来一遍，这里必须封住。"""
        counters: dict[str, list] = {"sessions": [], "connections": []}
        with _TrackingServer(self._payloads(6), delay=0.05) as server:
            self._run(server, concurrency=3, counters=counters)
            connections = server.connections - counters["connections"][-1] if counters["connections"] else server.connections
        created = len(counters["sessions"])
        self.assertLess(created, 6, f"6 个分片建了 {created} 条会话，说明没有按线程复用")
        self.assertLessEqual(created, 1 + 3, f"会话数 {created} 超过「播放列表 1 条 + 每线程 1 条」")
        self.assertLessEqual(connections, 5, "连接数偏高，分片之间没有复用连接")

    def test_concurrency_affects_hls_sessions(self) -> None:
        counters: dict[str, list] = {"sessions": [], "connections": []}
        with _TrackingServer(self._payloads(8), delay=0.05) as server:
            self._run(server, concurrency=1, counters=counters)
        self.assertLessEqual(len(counters["sessions"]), 2, "并发数=1 时不该开出多条会话")


class DownloadFilePathTests(unittest.TestCase):
    """download_file 的两条落盘路径：curl_cffi 回调（分片用）与流式读取（要按字节报进度时用）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _task(self, **overrides: object) -> DownloadTask:
        return DownloadTask(url="http://127.0.0.1/unused", mode="direct", output_dir=self.root, retries=0, **overrides)  # type: ignore[arg-type]

    def test_progress_path_writes_whole_file(self) -> None:
        payload = b"z" * 8192
        updates: list[tuple[float, str]] = []
        with _TrackingServer({"file.bin": payload}) as server:
            task = self._task()
            with http_client.build_session(task) as session:
                path, written = download_one(
                    session,
                    server.url("file.bin"),
                    self.root / "file.bin",
                    task,
                    lambda _m: None,
                    Event(),
                    lambda ratio, name: updates.append((ratio, name)),
                )
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(written, len(payload))
        self.assertTrue(updates)
        self.assertEqual(updates[-1][0], 1.0)

    def test_failed_download_leaves_no_files(self) -> None:
        with _TrackingServer({}) as server:
            task = self._task()
            with http_client.build_session(task) as session, self.assertRaises(RuntimeError):
                download_one(session, server.url("missing.bin"), self.root / "missing.bin", task, lambda _m: None, Event())
        self.assertFalse((self.root / "missing.bin").exists(), "失败后不该留下正式文件")
        self.assertFalse((self.root / "missing.bin.part").exists(), "什么都没下到就不该留 .part")


class Aria2ConcurrencyTests(unittest.TestCase):
    def test_command_carries_max_concurrent_downloads(self) -> None:
        session = Aria2Session(Path(tempfile.mkdtemp(prefix="aria2_cmd_")), max_concurrent_downloads=worker_count(8))
        with mock.patch("video_loader.services.aria2.find_aria2c", return_value="/usr/bin/aria2c"):
            command = session.build_command()
        self.assertIn("--max-concurrent-downloads=8", command)

    def test_command_omits_the_option_when_not_set(self) -> None:
        session = Aria2Session(Path(tempfile.mkdtemp(prefix="aria2_cmd_")))
        with mock.patch("video_loader.services.aria2.find_aria2c", return_value="/usr/bin/aria2c"):
            command = session.build_command()
        self.assertFalse([item for item in command if item.startswith("--max-concurrent-downloads")])

    def test_magnet_passes_task_concurrency(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="magnet_concurrency_")) / "out"
        magnet = "magnet:?xt=urn:btih:" + "a" * 40
        task = DownloadTask(url=magnet, mode="magnet", output_dir=out, concurrency=MAX_CONCURRENCY + 99)
        with mock.patch("video_loader.downloaders.magnet.Aria2Session") as factory:
            factory.side_effect = RuntimeError("测试里不真的启动 aria2c")
            result = MagnetDownloader().download(task, lambda *_a: None, lambda _m: None, Event())

        self.assertFalse(result.success)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs["max_concurrent_downloads"], MAX_CONCURRENCY)


if __name__ == "__main__":
    unittest.main()
