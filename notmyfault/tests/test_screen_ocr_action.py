"""screen_ocr 的参数、裁剪，以及识别或截图不可用时的失败。"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from notmyfault.plugin_api import PlatformServiceError
from notmyfault.security.plugin_schema import validate_plugin_meta
from notmyfault.security.plugins import scan_plugin_capabilities


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "screen_ocr"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("screen_ocr_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status(available, reason=None):
    return SimpleNamespace(available=available, reason=reason, backend=None, degraded=False)


def test_manifest_is_cross_platform_and_declares_screen_capabilities():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    valid, errors = validate_plugin_meta(meta, "action")
    assert valid, errors
    assert meta["id"] == "screen_ocr"
    assert meta["name"] == "屏幕文字识别"
    assert meta["platforms"] == ["windows", "linux"]
    assert meta["requires_capabilities"] == ["screen.capture", "screen.ocr"]
    assert [item["name"] for item in meta["outputs"]] == ["text", "language"]
    used = scan_plugin_capabilities(str(PLUGIN_ROOT / "action.py"))
    assert "native_api" in used
    assert used <= set(meta["permissions"])


def test_rejects_unknown_mode_language_and_region_size():
    module = load_module()
    with pytest.raises(ValueError, match="未知识别范围"):
        module.run({}, {"mode": "window"})
    with pytest.raises(ValueError, match="语言代码"):
        module.run({}, {"language": "eng;rm"})
    with pytest.raises(ValueError, match="至少为 1"):
        module.run({}, {"mode": "region", "x": 0, "y": 0, "width": 0, "height": 10})
    with pytest.raises(ValueError, match="不能为负数"):
        module.run({}, {"mode": "region", "x": -1, "y": 0, "width": 2, "height": 2})
    with pytest.raises(ValueError, match="距左边"):
        module.run({}, {"mode": "region", "x": True, "y": 0, "width": 2, "height": 2})


def test_missing_ocr_does_not_capture(monkeypatch):
    module = load_module()
    calls = []

    class FakeServices:
        def capability(self, name):
            assert name == "screen.ocr"
            return _status(False, "未安装 tesseract")

        def capture_screen(self, *args):
            calls.append("capture")

        def recognize_image(self, *args):
            calls.append("ocr")

    monkeypatch.setattr(module, "platform_services", FakeServices)
    with pytest.raises(RuntimeError, match="未安装 tesseract"):
        module.run({}, {"language": "eng"})
    assert calls == []


def test_linux_region_is_cropped_and_missing_capture_stops(monkeypatch):
    module = load_module()
    seen = {}

    class FakeServices:
        def __init__(self):
            self.capture_available = True

        def capability(self, name):
            if name == "screen.ocr":
                return _status(True)
            if name == "screen.capture":
                if self.capture_available:
                    return _status(True)
                return _status(False, "未安装 gnome-screenshot")
            raise AssertionError(name)

        def capture_screen(self, path, mode, fmt):
            assert mode == "fullscreen"
            assert fmt == "png"
            Image.new("RGB", (20, 10), "white").save(path, "PNG")
            return path

        def recognize_image(self, path, language):
            with Image.open(path) as image:
                seen["size"] = image.size
                seen["language"] = language
            return "你好"

    services = FakeServices()
    monkeypatch.setattr(module, "platform_services", lambda: services)
    monkeypatch.setattr(module.os, "name", "posix")

    assert module.run({}, {"mode": "region", "x": 1.0, "y": 2, "width": 3, "height": 4}) == {
        "text": "你好",
        "language": "chi_sim+eng",
    }
    assert seen == {"size": (3, 4), "language": "chi_sim+eng"}

    with pytest.raises(ValueError, match="超出截图范围"):
        module.run({}, {"mode": "region", "x": 18, "y": 0, "width": 5, "height": 1, "language": "eng"})

    services.capture_available = False
    seen.clear()
    with pytest.raises(RuntimeError, match="未安装 gnome-screenshot"):
        module.run({}, {"language": "eng"})
    assert seen == {}


def test_recognize_failure_uses_service_reason(monkeypatch):
    module = load_module()

    class FakeServices:
        def capability(self, name):
            if name == "screen.ocr":
                return _status(True)
            if name == "screen.capture":
                return _status(True)
            raise AssertionError(name)

        def capture_screen(self, path, mode, fmt):
            Image.new("RGB", (2, 2), "white").save(path, "PNG")
            return path

        def recognize_image(self, path, language):
            raise PlatformServiceError("screen.ocr", "backend_failed", "Error opening data file")

    monkeypatch.setattr(module, "platform_services", FakeServices)
    monkeypatch.setattr(module.os, "name", "posix")
    with pytest.raises(RuntimeError, match="Error opening data file"):
        module.run({}, {"language": "chi_sim"})


def test_windows_region_uses_local_capture(monkeypatch):
    module = load_module()
    captured = []

    def fake_capture(destination, region):
        captured.append(region)
        Image.new("RGB", (region[2], region[3]), "white").save(destination, "PNG")

    class FakeServices:
        def capability(self, name):
            assert name == "screen.ocr"
            return _status(True)

        def capture_screen(self, *args):
            raise AssertionError("Windows 不走 Linux 截图服务")

        def recognize_image(self, path, language):
            with Image.open(path) as image:
                assert image.size == (3, 4)
            assert language == "eng"
            return ""

    monkeypatch.setattr(module.os, "name", "nt")
    monkeypatch.setattr(module, "_capture_windows", fake_capture)
    monkeypatch.setattr(module, "platform_services", FakeServices)
    assert module.run({}, {
        "mode": "region",
        "x": 5,
        "y": 6,
        "width": 3,
        "height": 4,
        "language": "eng",
    }) == {"text": "", "language": "eng"}
    assert captured == [(5, 6, 3, 4)]
