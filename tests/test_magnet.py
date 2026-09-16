from __future__ import annotations

import hashlib
import secrets
import socket
import struct
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from video_loader.downloaders.magnet import (  # noqa: E402
    MagnetDownloader,
    is_magnet_url,
    magnet_error,
    split_magnet_lines,
)
from video_loader.models import DownloadResult, DownloadTask  # noqa: E402
from video_loader.services.aria2 import Aria2Session, find_aria2c, wait_for_gid  # noqa: E402

CONTENT = (b"VIDEO-LOADER-MAGNET-SEED " * 64)[: 4 * 1024]
FILENAME = "hello.txt"
PIECE_LENGTH = 16384
META_PIECE = 16384


def _benc(obj) -> bytes:
    if isinstance(obj, dict):
        return b"d" + b"".join(_benc(k) + _benc(v) for k, v in sorted(obj.items())) + b"e"
    if isinstance(obj, int):
        return b"i" + str(obj).encode() + b"e"
    if isinstance(obj, (str, bytes)):
        raw = obj.encode() if isinstance(obj, str) else obj
        return str(len(raw)).encode() + b":" + raw
    if isinstance(obj, list):
        return b"l" + b"".join(_benc(item) for item in obj) + b"e"
    raise TypeError(type(obj))


def _bdec(buf: bytes, i: int = 0):
    char = buf[i:i + 1]
    if char == b"d":
        i += 1
        out = {}
        while buf[i:i + 1] != b"e":
            key, i = _bdec(buf, i)
            value, i = _bdec(buf, i)
            out[key.decode() if isinstance(key, bytes) else key] = value
        return out, i + 1
    if char == b"l":
        i += 1
        out = []
        while buf[i:i + 1] != b"e":
            value, i = _bdec(buf, i)
            out.append(value)
        return out, i + 1
    if char == b"i":
        end = buf.index(b"e", i)
        return int(buf[i + 1:end]), end + 1
    end = buf.index(b":", i)
    size = int(buf[i:end])
    return buf[end + 1:end + 1 + size], end + 1 + size


def build_torrent(content: bytes, name: str) -> tuple[bytes, bytes]:
    """返回 (info 字典的 bencode, info hash)。"""
    pieces = b"".join(
        hashlib.sha1(content[i:i + PIECE_LENGTH]).digest()
        for i in range(0, len(content), PIECE_LENGTH)
    )
    info_benc = _benc({
        "length": len(content),
        "name": name,
        "piece length": PIECE_LENGTH,
        "pieces": pieces,
    })
    return info_benc, hashlib.sha1(info_benc).digest()


class Tracker(BaseHTTPRequestHandler):
    """HTTP tracker，按 compact=1 返回 6 字节/peer 的二进制 peers。"""

    peer_addr: tuple[str, int] = ("127.0.0.1", 0)

    def do_GET(self) -> None:
        ip, port = self.peer_addr
        compact = socket.inet_aton(ip) + struct.pack("!H", port)
        body = b"d" + _benc("interval") + b"i30e" + _benc("peers") + _benc(compact) + b"e"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        # 必须带 Content-Length，否则 aria2 按空响应处理并报 bencode 解析失败
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # type: ignore[override]
        pass


def seeder_peer(info_hash: bytes, info_benc: bytes, content: bytes, conn: socket.socket) -> None:
    """模拟做种 peer：BEP 10 扩展握手 + BEP 9 ut_metadata + 响应 piece 请求。"""
    peer_id = b"-VL0001-" + secrets.token_bytes(12)
    n_pieces = max(1, (len(content) + PIECE_LENGTH - 1) // PIECE_LENGTH)

    def send(payload: bytes) -> None:
        conn.sendall(struct.pack("!I", len(payload)) + payload)

    def send_ext(ext_id: int, payload: bytes) -> None:
        send(struct.pack("!B", 20) + struct.pack("!B", ext_id) + payload)

    def recv_exact(size: int) -> bytes:
        buf = b""
        while len(buf) < size:
            chunk = conn.recv(size - len(buf))
            if not chunk:
                raise ConnectionError("peer 关闭连接")
            buf += chunk
        return buf

    try:
        conn.settimeout(30)
        handshake = recv_exact(68)
        if handshake[1:20] != b"BitTorrent protocol":
            # aria2 会先尝试 MSE 加密握手，失败后自动回退明文，这里直接断开即可
            return
        if handshake[28:48] != info_hash:
            raise ValueError("info hash 不匹配")
        reserved = bytearray(handshake[20:28])
        reserved[5] |= 0x10  # 声明支持扩展协议
        conn.sendall(b"\x13BitTorrent protocol" + bytes(reserved) + info_hash + peer_id)

        send_ext(0, _benc({"m": {"ut_metadata": 1}, "metadata_size": len(info_benc)}))
        bitfield = bytearray((n_pieces + 7) // 8)
        for index in range(n_pieces):  # 末尾字节的备用位必须为 0，否则 aria2 报 Invalid bitfield
            bitfield[index // 8] |= 0x80 >> (index % 8)
        send(struct.pack("!B", 5) + bytes(bitfield))
        send(struct.pack("!B", 1))  # unchoke

        my_ut_meta = 1
        peer_ut_meta = 1
        while True:
            (msg_len,) = struct.unpack("!I", recv_exact(4))
            if msg_len == 0:
                continue
            msg = recv_exact(msg_len)
            mid = msg[0]
            if mid == 20:  # 扩展消息
                ext_id = msg[1]
                payload = msg[2:]
                if ext_id == 0:
                    info, _ = _bdec(payload)
                    declared = info.get("m") or {}
                    if "ut_metadata" in declared:
                        peer_ut_meta = int(declared["ut_metadata"])
                elif ext_id == my_ut_meta:
                    request, _ = _bdec(payload)
                    if int(request.get("msg_type", 0)) == 0:
                        piece = int(request.get("piece", 0))
                        chunk = info_benc[piece * META_PIECE:(piece + 1) * META_PIECE]
                        if chunk:
                            send_ext(peer_ut_meta, _benc({
                                "msg_type": 1,
                                "piece": piece,
                                "total_size": len(info_benc),
                            }) + chunk)
                        else:
                            send_ext(peer_ut_meta, _benc({"msg_type": 2, "piece": piece}))
            elif mid == 6:  # request：<id=6><index><begin><length>
                index, begin, length = struct.unpack("!III", msg[1:13])
                offset = index * PIECE_LENGTH + begin
                end = min(offset + length, len(content))
                # piece 消息格式是 <id=7><index><begin><block>，没有 length 字段
                send(struct.pack("!B", 7) + struct.pack("!II", index, begin) + content[offset:end])
            elif mid == 2:  # interested
                send(struct.pack("!B", 1))  # unchoke
            elif mid == 8:  # cancel
                pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


class _FakeSession:
    """按预设序列返回 tellStatus 结果的假 session，用于单测 wait_for_gid 的跟随逻辑。"""

    def __init__(self, statuses: list[dict | Exception]) -> None:
        self.statuses = list(statuses)
        self.log_callback = None
        self.queried: list[str] = []

    def call(self, method: str, params: list) -> dict:
        assert method == "aria2.tellStatus", method
        self.queried.append(str(params[0]))
        value = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(value, Exception):
            raise value
        return value


class MagnetUtilityTests(unittest.TestCase):
    def test_is_magnet_url(self) -> None:
        self.assertTrue(is_magnet_url("magnet:?xt=urn:btih:abc"))
        self.assertTrue(is_magnet_url("  MAGNET:?xt=urn:btih:abc  "))
        self.assertFalse(is_magnet_url("https://example.com/a.mp4"))
        self.assertFalse(is_magnet_url(""))

    def test_find_aria2c(self) -> None:
        found = find_aria2c()
        if found is None:
            self.skipTest("未安装 aria2c（brew install aria2）")
        self.assertTrue(Path(found).is_file(), f"aria2c 路径不存在：{found}")


class WaitForGidTests(unittest.TestCase):
    def test_follows_magnet_metadata_gid(self) -> None:
        """磁力链接先只下载元信息（GID 立即 complete 且有 followedBy），必须跟随到真正的下载任务。"""
        session = _FakeSession([
            {"status": "complete", "followedBy": ["gid-real"]},
            {"status": "active", "completedLength": "0", "totalLength": "100"},
            {"status": "complete", "completedLength": "100", "totalLength": "100"},
        ])
        result = wait_for_gid(session, "gid-meta", poll_interval=0, timeout=10)  # type: ignore[arg-type]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(session.queried[0], "gid-meta")
        self.assertEqual(session.queried[-1], "gid-real")

    def test_returns_error_status(self) -> None:
        session = _FakeSession([{"status": "error", "errorMessage": "boom"}])
        result = wait_for_gid(session, "gid", poll_interval=0, timeout=10)  # type: ignore[arg-type]
        self.assertEqual(result["status"], "error")

    def test_retries_when_followed_gid_not_yet_registered(self) -> None:
        """aria2 新建的下载 GID 可能晚几百毫秒注册，不能把 "not found" 当成下载失败。"""
        session = _FakeSession([
            {"status": "complete", "followedBy": ["gid-real"]},
            RuntimeError("aria2 RPC 错误：GID#gid-real is not found"),
            {"status": "complete", "completedLength": "10", "totalLength": "10"},
        ])
        result = wait_for_gid(session, "gid-meta", poll_interval=0, timeout=10)  # type: ignore[arg-type]
        self.assertEqual(result["status"], "complete")

    def test_metadata_phase_logs_heartbeat(self) -> None:
        """元信息阶段 totalLength 为 0，必须输出心跳日志，否则界面长时间无任何反馈。"""
        session = _FakeSession([
            {"status": "active", "totalLength": "0", "connections": "2", "numSeeders": "1"},
            {"status": "complete", "completedLength": "5", "totalLength": "5"},
        ])
        logs: list[str] = []
        session.log_callback = logs.append  # type: ignore[attr-defined]
        wait_for_gid(session, "gid", poll_interval=0, timeout=10, heartbeat_interval=0)  # type: ignore[arg-type]
        self.assertTrue(any("等待种子元信息" in line for line in logs), logs)
        self.assertTrue(any("已连接 2 个 peer" in line for line in logs), logs)

    def test_progress_uses_top_level_seeder_count(self) -> None:
        """种籽数来自 tellStatus 顶层的 numSeeders，不是 bitTorrent.seederCount。"""
        session = _FakeSession([
            {"status": "active", "totalLength": "100", "completedLength": "50", "numSeeders": "3", "downloadSpeed": "2048"},
            {"status": "complete", "completedLength": "100", "totalLength": "100"},
        ])
        logs: list[str] = []
        session.log_callback = logs.append  # type: ignore[attr-defined]
        wait_for_gid(session, "gid", poll_interval=0, timeout=10)  # type: ignore[arg-type]
        progress_logs = [line for line in logs if line.startswith("进度")]
        self.assertEqual(len(progress_logs), 1, logs)
        self.assertIn("种籽 3", progress_logs[0])
        self.assertIn("2 KiB/s", progress_logs[0])

    def test_timeout_message_reports_minutes(self) -> None:
        session = _FakeSession([{"status": "active", "totalLength": "0"}])
        with self.assertRaises(RuntimeError) as ctx:
            wait_for_gid(session, "gid", poll_interval=0, timeout=0)  # type: ignore[arg-type]
        self.assertIn("超时", str(ctx.exception))


class MagnetLineParsingTests(unittest.TestCase):
    VALID = "magnet:?xt=urn:btih:" + "a" * 40

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        text = f"# 每行一个磁力链接\n\n  {self.VALID}  \n"
        self.assertEqual(split_magnet_lines(text), [self.VALID])

    def test_valid_links_pass_validation(self) -> None:
        self.assertIsNone(magnet_error(self.VALID))
        self.assertIsNone(magnet_error("magnet:?xt=urn:btih:" + "a" * 32))
        self.assertIsNone(magnet_error("magnet:?xt=urn:btmh:" + "b" * 68))

    def test_non_magnet_url_is_rejected(self) -> None:
        problem = magnet_error("https://example.com/a.mp4")
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("magnet", problem)

    def test_placeholder_style_link_is_rejected_locally(self) -> None:
        """占位/占位样式的 btih 必须在本地就被拒绝，不能等到 aria2 报 No URI to download.。"""
        problem = magnet_error("magnet:?xt=urn:btih:XXXXXXX")
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("btih", problem)


class MagnetInputValidationTests(unittest.TestCase):
    """输入校验必须在启动 aria2c 之前完成（这些用例不需要安装 aria2）。"""

    def _download(self, url: str) -> DownloadResult:
        out = Path(tempfile.mkdtemp(prefix="magnet_input_")) / "out"
        task = DownloadTask(url=url, mode="magnet", output_dir=out)
        return MagnetDownloader().download(task, lambda value, message: None, lambda message: None, threading.Event())

    def test_comment_only_input_gets_clear_error(self) -> None:
        """界面写入的注释示例被直接提交时，应给出明确提示而不是 aria2 的 No URI to download.。"""
        hint = "# 每行一个磁力链接，例如：\n# magnet:?xt=urn:btih:" + "a" * 40 + "\n"
        result = self._download(hint)
        self.assertFalse(result.success)
        self.assertIn("请输入至少一个", " ".join(result.errors))

    def test_placeholder_magnet_rejected_locally(self) -> None:
        result = self._download("magnet:?xt=urn:btih:XXXXXXX（每行一个磁力链接）")
        self.assertFalse(result.success)
        self.assertIn("btih", " ".join(result.errors))


class Aria2SessionTests(unittest.TestCase):
    def test_start_reports_early_exit_and_keeps_no_process(self) -> None:
        workdir = Path(tempfile.mkdtemp(prefix="aria2_start_"))
        session = Aria2Session(workdir)
        with mock.patch("video_loader.services.aria2.find_aria2c", return_value=sys.executable):
            with self.assertRaises(RuntimeError) as ctx:
                session.start()
        self.assertIn("立即退出", str(ctx.exception))
        self.assertIsNone(session.process, "启动失败后不应残留进程引用")
        session.stop()  # 无进程时应安全退出


class MagnetEndToEndTests(unittest.TestCase):
    """端到端：本地 tracker + 本地做种 peer + magnet 链接，走项目 MagnetDownloader 真实下载。"""

    @classmethod
    def setUpClass(cls) -> None:
        if find_aria2c() is None:
            raise unittest.SkipTest("aria2c 不可用")

        cls.tmp = Path(tempfile.mkdtemp(prefix="aria2_magnet_e2e_"))
        cls.info_benc, cls.info_hash = build_torrent(CONTENT, FILENAME)

        cls.seeder_sock = socket.socket()
        cls.seeder_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.seeder_sock.bind(("127.0.0.1", 0))
        cls.seeder_sock.listen(8)
        seeder_port = cls.seeder_sock.getsockname()[1]

        def seeder_loop() -> None:
            while True:
                try:
                    conn, _addr = cls.seeder_sock.accept()
                except OSError:
                    return
                threading.Thread(
                    target=seeder_peer,
                    args=(cls.info_hash, cls.info_benc, CONTENT, conn),
                    daemon=True,
                ).start()

        threading.Thread(target=seeder_loop, daemon=True).start()

        Tracker.peer_addr = ("127.0.0.1", seeder_port)
        cls.tracker = HTTPServer(("127.0.0.1", 0), Tracker)
        cls.tracker_port = cls.tracker.server_address[1]
        threading.Thread(target=cls.tracker.serve_forever, daemon=True).start()

        cls.magnet = (
            f"magnet:?xt=urn:btih:{cls.info_hash.hex()}&dn={FILENAME}"
            f"&tr=http://127.0.0.1:{cls.tracker_port}/announce"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tracker.server_close()
        cls.seeder_sock.close()

    def test_magnet_download_via_local_seeder(self) -> None:
        output_dir = self.tmp / "out"
        task = DownloadTask(url=self.magnet, mode="magnet", output_dir=output_dir, timeout=30, retries=0)
        logs: list[str] = []
        cancel = threading.Event()
        holder: dict[str, DownloadResult | None] = {"result": None}

        def run() -> None:
            holder["result"] = MagnetDownloader().download(
                task, lambda value, message: None, logs.append, cancel
            )

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=120)
        if thread.is_alive():
            cancel.set()
            thread.join(timeout=10)
            self.fail("磁力下载超过 120 秒未完成（日志：%s）" % "\n".join(logs))

        result = holder["result"]
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.success, f"{result.message} {result.errors}")
        downloaded = output_dir / FILENAME
        self.assertTrue(downloaded.is_file(), "下载文件不存在")
        self.assertEqual(downloaded.read_bytes(), CONTENT, "下载内容与种子内容不一致")
        self.assertFalse((output_dir / f"{FILENAME}.aria2").exists(), "aria2 控制文件未清理")


if __name__ == "__main__":
    unittest.main()
