"""界面层的模式映射与磁力输入框提示（不需要 Tk 窗口，可在无显示环境运行）。"""
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
    """只提供 _on_mode_change 需要的两个属性。"""

    def __init__(self, mode_label: str, url_content: str = "") -> None:
        self.mode_var = _StubVar(mode_label)
        self.url_box = _StubBox(url_content)


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

    def test_other_modes_do_not_touch_url_box(self) -> None:
        for label, mode in app_module.LABEL_TO_MODE.items():
            if mode == "magnet":
                continue
            with self.subTest(label=label):
                app = _StubApp(label, "https://example.com/video.m3u8")
                app_module.VideoLoaderApp._on_mode_change(app, label)  # type: ignore[arg-type]
                self.assertEqual(app.url_box.content, "https://example.com/video.m3u8")


if __name__ == "__main__":
    unittest.main()
