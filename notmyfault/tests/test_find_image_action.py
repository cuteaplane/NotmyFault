"""find_image 的清单、像素匹配、区域校验，以及截图失败或未找到时的行为。"""

import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from notmyfault.core.workflow import ActionCancellation
from notmyfault.security.plugin_schema import validate_plugin_meta
from notmyfault.security.plugins import scan_plugin_capabilities


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "find_image"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("find_image_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status(available, reason=None):
    return SimpleNamespace(available=available, reason=reason, backend=None, degraded=False)


def _write_rgb(path: Path, size, color="white", patches=()):
    image = Image.new("RGB", size, color)
    for box, patch_color in patches:
        patch = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), patch_color)
        image.paste(patch, (box[0], box[1]))
    image.save(path, "PNG")
    return path


def test_manifest_declares_capture_and_context_apis():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    valid, errors = validate_plugin_meta(meta, "action")
    assert valid, errors
    assert meta["id"] == "find_image"
    assert meta["name"] == "屏幕找图"
    assert meta["package_name"] == "io.github.notmyfault.find_image"
    assert meta["platforms"] == ["windows", "linux"]
    assert meta["requires_capabilities"] == ["screen.capture"]
    assert meta["execution_api"] == "context-v1"
    assert meta["cancellation_api"] == "runtime-v1"
    assert meta["version"] == "1.0"
    assert meta["version_code"] == 1
    assert [item["name"] for item in meta["outputs"]] == [
        "found", "x", "y", "width", "height", "center_x", "center_y",
    ]
    used = scan_plugin_capabilities(str(PLUGIN_ROOT / "action.py"))
    assert "native_api" in used
    assert used <= set(meta["permissions"])


def test_exact_match_returns_screen_coords(tmp_path, monkeypatch):
    module = load_module()
    template = tmp_path / "tpl.png"
    _write_rgb(template, (3, 2), "red")

    class FakeServices:
        def capability(self, name):
            assert name == "screen.capture"
            return _status(True)

        def capture_screen(self, path, mode, fmt):
            assert mode == "fullscreen"
            assert fmt == "png"
            _write_rgb(
                Path(path),
                (10, 8),
                "white",
                patches=[((4, 3, 7, 5), "red")],
            )
            return path

    monkeypatch.setattr(module, "platform_services", FakeServices)
    monkeypatch.setattr(module.os, "name", "posix")
    result = module.run_with_context(
        {},
        {"template_path": str(template), "if_not_found": "error"},
        {},
    )
    assert result == {
        "found": True,
        "x": 4,
        "y": 3,
        "width": 3,
        "height": 2,
        "center_x": 5,
        "center_y": 4,
    }


def test_region_offsets_are_added_on_linux(tmp_path, monkeypatch):
    module = load_module()
    template = tmp_path / "tpl.png"
    _write_rgb(template, (2, 2), "blue")

    class FakeServices:
        def capability(self, name):
            return _status(True)

        def capture_screen(self, path, mode, fmt):
            _write_rgb(
                Path(path),
                (20, 10),
                "white",
                patches=[((5, 4, 7, 6), "blue")],
            )
            return path

    monkeypatch.setattr(module, "platform_services", FakeServices)
    monkeypatch.setattr(module.os, "name", "posix")
    result = module.run_with_context(
        {},
        {
            "template_path": str(template),
            "mode": "region",
            "x": 3,
            "y": 2,
            "width": 8,
            "height": 6,
        },
        {},
    )
    # 全屏图里补丁在 (5,4)；裁成距左 3、距上 2 后匹配点相对区域为 (2,2)
    assert result["found"] is True
    assert result["x"] == 5
    assert result["y"] == 4


def test_windows_capture_origin_is_applied(tmp_path, monkeypatch):
    module = load_module()
    template = tmp_path / "tpl.png"
    _write_rgb(template, (2, 1), "green")
    captured = []

    def fake_capture(destination, region):
        captured.append(region)
        # 不用 pathlib.Path：把 os.name 改成 nt 后 Path 会去建 WindowsPath
        image = Image.new("RGB", (6, 4), "white")
        image.paste(Image.new("RGB", (2, 1), "green"), (1, 2))
        image.save(destination, "PNG")
        return (-100, 50)

    monkeypatch.setattr(module, "_capture_windows", fake_capture)

    def fake_capture_once(services, directory, region):
        image_path = module.os.path.join(directory, "screen.png")
        origin_x, origin_y = module._capture_windows(image_path, region)
        return image_path, origin_x, origin_y

    monkeypatch.setattr(module, "_capture_once", fake_capture_once)
    monkeypatch.setattr(
        module,
        "platform_services",
        lambda: SimpleNamespace(capability=lambda name: _status(True)),
    )
    result = module.run_with_context(
        {},
        {
            "template_path": str(template),
            "mode": "region",
            "x": 10,
            "y": 20,
            "width": 6,
            "height": 4,
        },
        {},
    )
    assert captured == [(10, 20, 6, 4)]
    assert result["found"] is True
    assert result["x"] == -99
    assert result["y"] == 52


def test_not_found_empty_and_error(tmp_path, monkeypatch):
    module = load_module()
    template = tmp_path / "tpl.png"
    _write_rgb(template, (2, 2), "red")

    class FakeServices:
        def capability(self, name):
            return _status(True)

        def capture_screen(self, path, mode, fmt):
            Image.new("RGB", (8, 8), "white").save(path, "PNG")
            return path

    monkeypatch.setattr(module, "platform_services", FakeServices)
    monkeypatch.setattr(module.os, "name", "posix")
    assert module.run_with_context(
        {},
        {"template_path": str(template), "if_not_found": "empty"},
        {},
    ) == {"found": False}
    with pytest.raises(RuntimeError, match="未在屏幕中找到"):
        module.run_with_context(
            {},
            {"template_path": str(template), "if_not_found": "error"},
            {},
        )


def test_bad_template_path_and_region_validation(tmp_path, monkeypatch):
    module = load_module()
    with pytest.raises(ValueError, match="请填写模板图片路径"):
        module.run_with_context({}, {}, {})
    with pytest.raises(ValueError, match="不存在"):
        module.run_with_context({}, {"template_path": str(tmp_path / "missing.png")}, {})
    weird = tmp_path / "a.gif"
    weird.write_bytes(b"gif")
    with pytest.raises(ValueError, match="png、jpg、jpeg 或 bmp"):
        module.run_with_context({}, {"template_path": str(weird)}, {})
    template = tmp_path / "ok.png"
    _write_rgb(template, (1, 1), "black")
    with pytest.raises(ValueError, match="未知查找范围"):
        module.run_with_context(
            {},
            {"template_path": str(template), "mode": "window"},
            {},
        )
    with pytest.raises(ValueError, match="至少为 1"):
        module.run_with_context(
            {},
            {
                "template_path": str(template),
                "mode": "region",
                "x": 0,
                "y": 0,
                "width": 0,
                "height": 2,
            },
            {},
        )
    with pytest.raises(ValueError, match="不能为负数"):
        module.run_with_context(
            {},
            {
                "template_path": str(template),
                "mode": "region",
                "x": -1,
                "y": 0,
                "width": 2,
                "height": 2,
            },
            {},
        )
    with pytest.raises(ValueError, match="距左边"):
        module.run_with_context(
            {},
            {
                "template_path": str(template),
                "mode": "region",
                "x": True,
                "y": 0,
                "width": 2,
                "height": 2,
            },
            {},
        )
    with pytest.raises(ValueError, match="查找时限不能为负数"):
        module.run_with_context(
            {},
            {"template_path": str(template), "timeout_seconds": -1},
            {},
        )
    with pytest.raises(ValueError, match="重试间隔"):
        module.run_with_context(
            {},
            {"template_path": str(template), "interval_seconds": 0.01},
            {},
        )


def test_template_larger_than_area(tmp_path, monkeypatch):
    module = load_module()
    template = tmp_path / "big.png"
    _write_rgb(template, (5, 5), "red")

    class FakeServices:
        def capability(self, name):
            return _status(True)

        def capture_screen(self, path, mode, fmt):
            Image.new("RGB", (3, 3), "white").save(path, "PNG")
            return path

    monkeypatch.setattr(module, "platform_services", FakeServices)
    monkeypatch.setattr(module.os, "name", "posix")
    with pytest.raises(ValueError, match="大于查找区域"):
        module.run_with_context({}, {"template_path": str(template)}, {})


def test_timeout_retries_until_found(tmp_path, monkeypatch):
    module = load_module()
    template = tmp_path / "tpl.png"
    _write_rgb(template, (2, 2), "red")
    calls = {"n": 0}

    class FakeServices:
        def capability(self, name):
            return _status(True)

        def capture_screen(self, path, mode, fmt):
            calls["n"] += 1
            if calls["n"] < 3:
                Image.new("RGB", (6, 6), "white").save(path, "PNG")
            else:
                _write_rgb(
                    Path(path),
                    (6, 6),
                    "white",
                    patches=[((1, 1, 3, 3), "red")],
                )
            return path

    monkeypatch.setattr(module, "platform_services", FakeServices)
    monkeypatch.setattr(module.os, "name", "posix")
    result = module.run_with_context(
        {},
        {
            "template_path": str(template),
            "timeout_seconds": 2,
            "interval_seconds": 0.1,
        },
        {},
    )
    assert result["found"] is True
    assert result["x"] == 1
    assert result["y"] == 1
    assert calls["n"] >= 3


def test_cancellation_stops_wait_loop(tmp_path, monkeypatch):
    module = load_module()
    template = tmp_path / "tpl.png"
    _write_rgb(template, (1, 1), "red")

    class FakeServices:
        def capability(self, name):
            return _status(True)

        def capture_screen(self, path, mode, fmt):
            Image.new("RGB", (4, 4), "white").save(path, "PNG")
            return path

    monkeypatch.setattr(module, "platform_services", FakeServices)
    monkeypatch.setattr(module.os, "name", "posix")
    cancel_event = threading.Event()
    cancellation = ActionCancellation(cancel_event)

    def cancel_soon():
        time.sleep(0.05)
        cancel_event.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(Exception, match="取消|超时"):
        module.run_with_context(
            {},
            {
                "template_path": str(template),
                "timeout_seconds": 5,
                "interval_seconds": 0.5,
                "if_not_found": "empty",
            },
            {"runtime": {"cancellation": cancellation}},
        )
    assert time.monotonic() - started < 2.0


def test_missing_capture_capability(tmp_path, monkeypatch):
    module = load_module()
    template = tmp_path / "tpl.png"
    _write_rgb(template, (1, 1), "black")

    class FakeServices:
        def capability(self, name):
            return _status(False, "未安装 gnome-screenshot")

        def capture_screen(self, *args):
            raise AssertionError("不应截图")

    monkeypatch.setattr(module, "platform_services", FakeServices)
    monkeypatch.setattr(module.os, "name", "posix")
    with pytest.raises(RuntimeError, match="未安装 gnome-screenshot"):
        module.run_with_context({}, {"template_path": str(template)}, {})
