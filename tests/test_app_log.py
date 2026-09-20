"""应用日志与数字输入框：退出原因诊断、空值兜底。"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import video_loader.app as app_module  # noqa: E402
from video_loader.services import app_log  # noqa: E402


class AppLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log_file = Path(self._tmp.name) / "nested" / "app.log"
        os.environ[app_log.LOG_ENV_VAR] = str(self.log_file)
        self.addCleanup(lambda: os.environ.pop(app_log.LOG_ENV_VAR, None))
        self.addCleanup(self._tmp.cleanup)

    def test_log_path_follows_env_override(self) -> None:
        self.assertEqual(app_log.log_path(), self.log_file)

    def test_lines_are_appended_with_pid_and_timestamp(self) -> None:
        app_log.log_line("启动")
        app_log.log_line("运行中（心跳）")
        content = self.log_file.read_text(encoding="utf-8")
        self.assertIn("启动", content)
        self.assertIn("运行中（心跳）", content)
        self.assertIn(f"[{os.getpid()}]", content)
        self.assertEqual(len(content.strip().splitlines()), 2)

    def test_logging_failure_never_raises(self) -> None:
        """日志目录不可写时也不能影响主流程。"""
        os.environ[app_log.LOG_ENV_VAR] = "/proc/nope/nowhere.log"
        app_log.log_line("不该抛异常")

    def test_ignore_sighup_does_not_raise(self) -> None:
        original = signal.getsignal(signal.SIGHUP)
        self.addCleanup(signal.signal, signal.SIGHUP, original)
        app_log.ignore_sighup()
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)


class NumberFieldTests(unittest.TestCase):
    """数字框绑 StringVar 后，取值必须容错：留空用默认值、非法值给明确提示。"""

    value_of = staticmethod(app_module.VideoLoaderApp._number_field_value)

    def test_empty_uses_default(self) -> None:
        self.assertEqual(self.value_of("", "并发数", minimum=1, default=4), 4)
        self.assertEqual(self.value_of("   ", "超时秒数", minimum=1, default=30), 30)
        self.assertEqual(self.value_of(None, "重试次数", minimum=0, default=2), 2)  # type: ignore[arg-type]

    def test_parses_numbers(self) -> None:
        self.assertEqual(self.value_of("8", "并发数", minimum=1, default=4), 8)
        self.assertEqual(self.value_of(8, "并发数", minimum=1, default=4), 8)  # type: ignore[arg-type]
        self.assertEqual(self.value_of("0", "重试次数", minimum=0, default=2), 0)

    def test_clamps_to_minimum(self) -> None:
        self.assertEqual(self.value_of("-3", "并发数", minimum=1, default=4), 1)

    def test_invalid_value_reports_field_name(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.value_of("abc", "并发数", minimum=1, default=4)
        self.assertIn("并发数", str(ctx.exception))
        self.assertIn("abc", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
