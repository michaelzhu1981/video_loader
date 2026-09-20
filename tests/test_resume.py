"""断点续传：`.part` 临时文件、Range 续下、以及重跑时复用已下好的分片。

这里的服务器真的实现 `Range: bytes=N-`（206/416），所以"续传有没有生效"是服务端看到的事实：
请求里有没有 Range、实际传了多少字节、最终文件内容对不对，都逐条断言。
"""
from __future__ import annotations

import http.server
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from pathlib import Path
from threading import Event
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_loader.downloaders.base import download_one, part_path  # noqa: E402
from video_loader.downloaders.hls import HlsDownloader  # noqa: E402
from video_loader.downloaders.segment_list import SegmentListDownloader  # noqa: E402
from video_loader.models import DownloadTask  # noqa: E402
from video_loader.services import http_client  # noqa: E402

CHUNK = 16 * 1024


def payload(tag: int, size: int = 512 * 1024) -> bytes:
    """每个分片内容不同，方便验证顺序和是否真的续对了位置。"""
    return bytes([65 + tag % 26]) * size


def m3u8(paths: list[str]) -> bytes:
    return ("\n".join(["#EXTM3U"] + [f"#EXTINF:1.0,\n{p}" for p in paths]) + "\n").encode("utf-8")


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        server: _RangeServer = self.server  # type: ignore[assignment]
        path = self.path.lstrip("/")
        headers = {key.lower(): value for key, value in self.headers.items()}
        with server.lock:
            server.request_log.append((path, headers))

        body = server.payloads.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start = 0
        raw_range = headers.get("range", "")
        if raw_range.startswith("bytes="):
            head = raw_range[len("bytes=") :].split("-", 1)[0]
            start = int(head) if head.isdigit() else 0

        if start >= len(body):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(body)}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if start:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()

        sent = 0
        try:
            for offset in range(start, len(body), CHUNK):
                piece = body[offset : offset + CHUNK]
                self.wfile.write(piece)
                sent += len(piece)
                if server.chunk_delay:
                    time.sleep(server.chunk_delay)
        except (BrokenPipeError, ConnectionResetError):  # 客户端取消时正常发生
            pass
        finally:
            with server.lock:
                server.sent_bytes[path] += sent

    def handle_one_request(self) -> None:
        # 客户端取消时会 reset 连接，读下一行请求会抛错，属于预期情况
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 覆盖基类签名
        pass


class _RangeServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, payloads: dict[str, bytes], chunk_delay: float = 0.0) -> None:
        super().__init__(("127.0.0.1", 0), _RangeHandler)
        self.payloads = payloads
        self.chunk_delay = chunk_delay
        self.lock = threading.Lock()
        self.request_log: list[tuple[str, dict[str, str]]] = []
        self.sent_bytes: dict[str, int] = defaultdict(int)
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    def __enter__(self) -> "_RangeServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        self.server_close()

    def url(self, path: str) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/{path.lstrip('/')}"

    def ranges(self, path: str) -> list[str]:
        with self.lock:
            return [headers["range"] for seen, headers in self.request_log if seen == path and "range" in headers]


def count_requests(server: _RangeServer, path: str) -> int:
    with server.lock:
        return sum(1 for seen, _headers in server.request_log if seen == path)


def fake_combine(files: list[Path], output_path: Path, _log: object = None) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"".join(Path(item).read_bytes() for item in files))
    return output_path


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.payload = payload(1)
        self.half = self.payload[: len(self.payload) // 2]

    def _task(self, url: str, **overrides: object) -> DownloadTask:
        return DownloadTask(url=url, mode="segment_list", output_dir=self.root / "out", retries=0, **overrides)  # type: ignore[arg-type]

    def _session(self, task: DownloadTask):
        return http_client.build_session(task)

    def test_complete_file_is_reused_without_transferring_bytes(self) -> None:
        target = self.root / "out" / "_segments" / "segment-00000.ts"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload)
        with _RangeServer({"seg.ts": self.payload}) as server:
            task = self._task(server.url("seg.ts"))
            with self._session(task) as session:
                path, written = download_one(session, task.url, target, task, lambda _m: None, Event())

        self.assertEqual(path, target)
        self.assertEqual(written, 0, "完整文件不该再传字节")
        self.assertEqual(server.sent_bytes["seg.ts"], 0)
        self.assertEqual(self.payload, target.read_bytes())
        self.assertEqual(server.ranges("seg.ts"), [f"bytes={len(self.payload)}-"], "应先用 Range 问一次是否已下满")

    def test_truncated_file_is_resumed_from_its_size(self) -> None:
        target = self.root / "out" / "_segments" / "segment-00000.ts"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.half)
        with _RangeServer({"seg.ts": self.payload}) as server:
            task = self._task(server.url("seg.ts"))
            with self._session(task) as session:
                path, written = download_one(session, task.url, target, task, lambda _m: None, Event())

        self.assertEqual(path.read_bytes(), self.payload, "续传后文件内容必须和源一致")
        self.assertEqual(server.ranges("seg.ts"), [f"bytes={len(self.half)}-"])
        self.assertEqual(server.sent_bytes["seg.ts"], len(self.payload) - len(self.half), "只该补传后半截")
        self.assertEqual(written, len(self.payload) - len(self.half))

    def test_part_file_is_resumed(self) -> None:
        target = self.root / "out" / "_segments" / "segment-00000.ts"
        part = part_path(target)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(self.half)
        with _RangeServer({"seg.ts": self.payload}) as server:
            task = self._task(server.url("seg.ts"))
            with self._session(task) as session:
                path, _written = download_one(session, task.url, target, task, lambda _m: None, Event())

        self.assertEqual(path, target)
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertFalse(part.exists(), "下完之后 .part 不该留在磁盘上")

    def test_oversized_local_file_is_redownloaded(self) -> None:
        """本地那份比服务端还大（旧版本/别处留下的）→ 416 且大小对不上 → 重下，不能当成功。"""
        target = self.root / "out" / "_segments" / "segment-00000.ts"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload + b"garbage")
        with _RangeServer({"seg.ts": self.payload}) as server:
            task = self._task(server.url("seg.ts"))
            with self._session(task) as session:
                path, _written = download_one(session, task.url, target, task, lambda _m: None, Event())

        self.assertEqual(path.read_bytes(), self.payload)

    def test_cancel_keeps_part_file_and_next_run_finishes_it(self) -> None:
        target = self.root / "out" / "big.bin"
        cancel = Event()
        first: list[float] = []

        def cancel_after_first_chunk(ratio: float, _name: str) -> None:
            first.append(ratio)
            cancel.set()

        with _RangeServer({"big.bin": self.payload}, chunk_delay=0.01) as server:
            task = self._task(server.url("big.bin"))
            with self._session(task) as session:
                with self.assertRaises(RuntimeError) as ctx:
                    download_one(
                        session,
                        task.url,
                        target,
                        task,
                        lambda _m: None,
                        cancel,
                        cancel_after_first_chunk,
                    )
            self.assertIn("取消", str(ctx.exception))
            partial = part_path(target).stat().st_size
            self.assertFalse(target.exists(), "取消时不该改名成正式文件")
            self.assertGreater(partial, 0, "取消时应保留已下到的内容供续传")
            self.assertLess(partial, len(self.payload))

            # 第二次跑：从 .part 的大小接着下
            with self._session(task) as session:
                path, _written = download_one(session, task.url, target, task, lambda _m: None, Event())

        self.assertEqual(server.ranges("big.bin"), [f"bytes={partial}-"])
        self.assertEqual(path.read_bytes(), self.payload)
        # 服务端计数会把"写进 socket 但客户端已不再读"的部分也算上，所以只断言"至少把整份都发过"
        self.assertGreaterEqual(server.sent_bytes["big.bin"], len(self.payload))

    def test_failed_download_leaves_no_empty_part(self) -> None:
        target = self.root / "out" / "missing.bin"
        with _RangeServer({}) as server:
            task = self._task(server.url("missing.bin"))
            with self._session(task) as session:
                with self.assertRaises(RuntimeError):
                    download_one(session, task.url, target, task, lambda _m: None, Event())
        self.assertFalse(part_path(target).exists())
        self.assertFalse(target.exists())

    def test_resume_disabled_ignores_existing_files(self) -> None:
        target = self.root / "out" / "_segments" / "segment-00000.ts"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.half)
        with _RangeServer({"seg.ts": self.payload}) as server:
            task = self._task(server.url("seg.ts"), resume=False)
            with self._session(task) as session:
                path, _written = download_one(session, task.url, target, task, lambda _m: None, Event())

        self.assertEqual(server.ranges("seg.ts"), [], "关掉续传就不该发 Range 请求")
        self.assertEqual(server.sent_bytes["seg.ts"], len(self.payload), "关掉续传就是整份重下")


class SegmentReuseTests(unittest.TestCase):
    """"取消后重跑"在下载器层面到底省了多少：只有缺失的分片会被真的取一遍。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.count = 6
        self.body = {f"seg{i}.ts": payload(i) for i in range(self.count)}
        self.payloads = dict(self.body)
        self.payloads["list.m3u8"] = m3u8([f"seg{i}.ts" for i in range(self.count)])

    def _prune(self, out: Path, missing: set[int], truncated: set[int] | None = None) -> None:
        truncated = truncated or set()
        segment_dir = out / "_segments"
        segment_dir.mkdir(parents=True, exist_ok=True)
        for index in range(self.count):
            if index in missing:
                continue
            data = self.body[f"seg{index}.ts"]
            if index in truncated:
                part_path(segment_dir / f"segment-{index:05d}.ts").write_bytes(data[: len(data) // 4])
            else:
                (segment_dir / f"segment-{index:05d}.ts").write_bytes(data)

    def test_segment_list_reuses_finished_segments_and_resumes_partials(self) -> None:
        out = self.root / "out"
        self._prune(out, missing={4}, truncated={2})
        result = None
        with _RangeServer(self.payloads, chunk_delay=0.0) as server:
            task = DownloadTask(
                url="\n".join(server.url(f"seg{i}.ts") for i in range(self.count)),
                mode="segment_list",
                output_dir=out,
                concurrency=3,
                combine_segments=False,
                retries=0,
            )
            logs: list[str] = []
            result = SegmentListDownloader().download(task, lambda *_a: None, logs.append, Event())

            self.assertTrue(result.success, result.errors)
            self.assertEqual(server.sent_bytes["seg4.ts"], len(self.body["seg4.ts"]), "缺失的那片要整片下")
            self.assertEqual(
                server.sent_bytes["seg2.ts"],
                3 * len(self.body["seg2.ts"]) // 4,
                "半截的那片只该补传剩下的 3/4",
            )
            self.assertEqual(server.ranges("seg2.ts"), [f"bytes={len(self.body['seg2.ts']) // 4}-"])
            for index in (0, 1, 3, 5):
                self.assertEqual(count_requests(server, f"seg{index}.ts"), 1, "已下好的分片只该被问一次")
                self.assertEqual(server.sent_bytes[f"seg{index}.ts"], 0, "已下好的分片不该重传")
            self.assertTrue(any("复用" in line for line in logs), logs)

        for index in range(self.count):
            self.assertEqual((out / "_segments" / f"segment-{index:05d}.ts").read_bytes(), self.body[f"seg{index}.ts"])

    def test_hls_second_run_only_fetches_what_is_missing(self) -> None:
        out = self.root / "hls"
        self._prune(out, missing={5})
        with _RangeServer(self.payloads) as server:
            task = DownloadTask(url=server.url("list.m3u8"), mode="hls", output_dir=out, concurrency=2, retries=0)
            with mock.patch("video_loader.downloaders.hls.combine_with_concat_demuxer", fake_combine):
                result = HlsDownloader().download(task, lambda *_a: None, lambda _m: None, Event())

            self.assertTrue(result.success, result.errors)
            self.assertEqual(server.sent_bytes["seg5.ts"], len(self.body["seg5.ts"]))
            for index in range(5):
                self.assertEqual(server.sent_bytes[f"seg{index}.ts"], 0, f"seg{index} 应该被复用而不是重下")

        merged = result.output_path
        assert merged is not None
        self.assertEqual(merged.read_bytes(), b"".join(self.body[f"seg{i}.ts"] for i in range(self.count)))


if __name__ == "__main__":
    unittest.main()
