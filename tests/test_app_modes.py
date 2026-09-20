"""界面层的模式映射与输入框提示（不需要 Tk 窗口，可在无显示环境运行）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import video_loader.app as app_module  # noqa: E402


class _StubBox:
    def __init__(self, content: str = "") -> None:
        self.content = content

    def get(self, _start: str, _end: str) -> str:
        return self.content

    def delete(self, _start: str, _end: str) -> None:
        self.content = ""

    def insert(self, _index: str, text: str) -> None:
        self.content = text


class _StubVar:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class _StubApp:
    """只提供 _on_mode_change 需要的属性。"""

    def __init__(self, mode_label: str, url_content: str = "") -> None:
        self.mode_var = _StubVar(mode_label)
        self.url_box = _StubBox(url_content)
        self.logs: list[str] = []

    def _log(self, message: str) -> None:
        self.logs.append(message)


class ModeMappingTests(unittest.TestCase):
    def test_every_mode_has_label_and_description(self) -> None:
        self.assertEqual(set(app_module.MODE_LABELS), set(app_module.MODE_DESCRIPTIONS))
        self.assertEqual(set(app_module.LABEL_TO_MODE.values()), set(app_module.MODE_LABELS))

    def test_label_to_mode_maps_labels_not_modes(self) -> None:
        """LABEL_TO_MODE 是 标签 -> 模式；用模式名当键去查会 KeyError（界面回调曾因此报错）。"""
        self.assertNotIn("magnet", app_module.LABEL_TO_MODE)
        self.assertEqual(app_module.LABEL_TO_MODE[app_module.MAGNET_LABEL], "magnet")

    def test_magnet_label_matches_menu_values(self) -> None:
        self.assertIn(app_module.MAGNET_LABEL, app_module.LABEL_TO_MODE)
        self.assertIn(app_module.MAGNET_LABEL, set(app_module.LABEL_TO_MODE.keys()))


class ModeChangeCallbackTests(unittest.TestCase):
    def test_switching_to_magnet_inserts_comment_hint(self) -> None:
        app = _StubApp(app_module.MAGNET_LABEL, "https://example.com/video.m3u8")
        app_module.VideoLoaderApp._on_mode_change(app, app_module.MAGNET_LABEL)  # type: ignore[arg-type]
        lines = [line for line in app.url_box.content.splitlines() if line.strip()]
        self.assertTrue(lines, "应为磁力模式写入提示")
        self.assertTrue(all(line.startswith("#") for line in lines), lines)

    def test_switching_to_magnet_keeps_existing_magnet_input(self) -> None:
        existing = "magnet:?xt=urn:btih:" + "a" * 40
        app = _StubApp(app_module.MAGNET_LABEL, existing)
        app_module.VideoLoaderApp._on_mode_change(app, app_module.MAGNET_LABEL)  # type: ignore[arg-type]
        self.assertEqual(app.url_box.content, existing)


class SniffModeHintTests(unittest.TestCase):
    def test_switching_to_sniff_replaces_example_m3u8_with_page_placeholder(self) -> None:
        app = _StubApp(app_module.SNIFF_LABEL, "https://example.com/video.m3u8")
        app_module.VideoLoaderApp._on_mode_change(app, app_module.SNIFF_LABEL)  # type: ignore[arg-type]
        self.assertEqual(app.url_box.content, app_module.SNIFF_PLACEHOLDER)
        self.assertEqual(app.logs, [app_module.SNIFF_HINT])

    def test_switching_to_sniff_fills_empty_box(self) -> None:
        app = _StubApp(app_module.SNIFF_LABEL, "")
        app_module.VideoLoaderApp._on_mode_change(app, app_module.SNIFF_LABEL)  # type: ignore[arg-type]
        self.assertEqual(app.url_box.content, app_module.SNIFF_PLACEHOLDER)

    def test_switching_to_sniff_keeps_user_page_url(self) -> None:
        existing = "https://example.org/watch/42"
        app = _StubApp(app_module.SNIFF_LABEL, existing)
        app_module.VideoLoaderApp._on_mode_change(app, app_module.SNIFF_LABEL)  # type: ignore[arg-type]
        self.assertEqual(app.url_box.content, existing)

    def test_sniff_label_exists_and_is_not_the_magnet_label(self) -> None:
        self.assertNotEqual(app_module.SNIFF_LABEL, app_module.MAGNET_LABEL)
        self.assertEqual(app_module.LABEL_TO_MODE[app_module.SNIFF_LABEL], "sniff")

    def test_other_modes_do_not_touch_url_box(self) -> None:
        # magnet 写入注释提示行，sniff 会把示例 m3u8 换成页面地址，两者都有专门用例覆盖。
        for label, mode in app_module.LABEL_TO_MODE.items():
            if mode in {"magnet", "sniff"}:
                continue
            with self.subTest(label=label):
                app = _StubApp(label, "https://example.com/video.m3u8")
                app_module.VideoLoaderApp._on_mode_change(app, label)  # type: ignore[arg-type]
                self.assertEqual(app.url_box.content, "https://example.com/video.m3u8")


class _StubTaskApp:
    """构造 DownloadTask 所需的最小控件集合（不创建 Tk 窗口）。"""

    def __init__(self, mode_label: str, url: str, quality: str = "", choices: list | None = None) -> None:
        self.mode_var = _StubVar(mode_label)
        self.url_box = _StubBox(url)
        self.output_dir_var = _StubVar("/tmp/video-loader-test")
        self.output_name_var = _StubVar("video.mp4")
        self.concurrency_var = _StubVar(4)
        self.timeout_var = _StubVar(30)
        self.retries_var = _StubVar(2)
        self.verify_ssl_var = _StubVar(True)
        self.combine_var = _StubVar(True)
        self.impersonate_var = _StubVar(True)
        self.browser_var = _StubVar(False)
        self.quality_var = _StubVar(quality)
        self.quality_choices = choices or []
        self.headers_box = _StubBox("User-Agent: Test")
        self.cookies_box = _StubBox("")
        self.logs: list[str] = []

    def _log(self, message: str) -> None:
        self.logs.append(message)


def _build_task(app: "_StubTaskApp"):
    return app_module.VideoLoaderApp._build_task(app)  # type: ignore[arg-type]


class BuildTaskTests(unittest.TestCase):
    def test_sniff_keeps_page_url_and_uses_chosen_quality(self) -> None:
        from video_loader.models import StreamVariant

        variant = StreamVariant(url="https://cdn.example.com/480p/video.m3u8", label="480p", resolution="854x480")
        app = _StubTaskApp(app_module.SNIFF_LABEL, "https://site.example/watch/1", quality="480p · 854x480", choices=[("480p · 854x480", variant)])
        task = _build_task(app)
        self.assertEqual(task.mode, "sniff")
        self.assertEqual(task.url, "https://site.example/watch/1")
        self.assertEqual(task.preferred_quality, "480p")
        self.assertEqual(task.selected_stream_url, variant.url)
        self.assertTrue(task.impersonate)
        self.assertFalse(task.use_browser)

    def test_sniff_without_parsed_quality_leaves_selection_empty(self) -> None:
        app = _StubTaskApp(app_module.SNIFF_LABEL, "https://site.example/watch/1", quality=app_module.AUTO_QUALITY_LABEL)
        task = _build_task(app)
        self.assertEqual(task.preferred_quality, "")
        self.assertEqual(task.selected_stream_url, "")

    def test_sniff_rejects_m3u8_link(self) -> None:
        app = _StubTaskApp(app_module.SNIFF_LABEL, "https://cdn.example.com/hls/video.m3u8")
        with self.assertRaises(ValueError) as ctx:
            _build_task(app)
        self.assertIn("HLS / m3u8", str(ctx.exception))

    def test_sniff_uses_first_address_of_multiple_lines(self) -> None:
        app = _StubTaskApp(app_module.SNIFF_LABEL, "https://site.example/a\n# 注释\nhttps://site.example/b")
        task = _build_task(app)
        self.assertEqual(task.url, "https://site.example/a")
        self.assertTrue(any("只处理第一个地址" in line for line in app.logs))

    def test_other_modes_keep_passed_url_untouched(self) -> None:
        app = _StubTaskApp(app_module.MODE_LABELS["hls"], "https://cdn.example.com/hls/video.m3u8")
        task = _build_task(app)
        self.assertEqual(task.mode, "hls")
        self.assertEqual(task.url, "https://cdn.example.com/hls/video.m3u8")


if __name__ == "__main__":
    unittest.main()
