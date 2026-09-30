"""mouse 动作的参数校验与 Linux 平台服务分发测试。"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from notmyfault.security.plugin_schema import validate_plugin_meta


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "mouse"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("mouse_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_requires_input_send_and_is_cross_platform():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    valid, errors = validate_plugin_meta(meta, "action")
    assert valid, errors
    assert meta["id"] == "mouse"
    assert meta["platforms"] == ["windows", "linux"]
    assert meta["requires_capabilities"] == ["input.send"]
    assert "native_api" in meta["permissions"]
    assert "external_binary" in meta["permissions"]
    assert "security" not in meta


def test_unknown_mode_rejected():
    module = load_module()
    with pytest.raises(ValueError, match="未知模式"):
        module.run({}, {"mode": "drag"})


def test_invalid_button_rejected():
    module = load_module()
    with pytest.raises(ValueError, match="不支持的鼠标按键"):
        module.run({}, {"mode": "click", "button": "side"})


def test_move_requires_integer_coords():
    module = load_module()
    with pytest.raises(ValueError, match="横坐标"):
        module.run({}, {"mode": "move", "x": "10", "y": 20})


def test_linux_move_and_click_dispatch_platform_services(monkeypatch):
    module = load_module()
    calls = []

    class FakeServices:
        def move_mouse(self, x, y, absolute=True):
            calls.append(("move", x, y, absolute))

        def click_mouse(self, button="left", clicks=1):
            calls.append(("click", button, clicks))

        def mouse_button(self, button, pressed):
            calls.append(("button", button, pressed))

        def scroll_mouse(self, direction, amount=3):
            calls.append(("scroll", direction, amount))

    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(module, "platform_services", FakeServices)

    assert module.run({}, {"mode": "move", "x": 100, "y": -20}) == {
        "mode": "move",
        "x": 100,
        "y": -20,
    }
    assert module.run({}, {"mode": "move_relative", "dx": -5, "dy": 8}) == {
        "mode": "move_relative",
        "x": -5,
        "y": 8,
    }
    assert module.run({}, {"mode": "double_click", "button": "right"}) == {
        "mode": "double_click",
        "button": "right",
    }
    assert module.run({}, {"mode": "press", "button": "middle"}) == {
        "mode": "press",
        "button": "middle",
    }
    assert module.run({}, {"mode": "release", "button": "middle"}) == {
        "mode": "release",
        "button": "middle",
    }
    assert module.run({}, {"mode": "scroll", "direction": "up", "amount": 2}) == {
        "mode": "scroll",
        "direction": "up",
        "amount": 2,
    }

    assert calls == [
        ("move", 100, -20, True),
        ("move", -5, 8, False),
        ("click", "right", 2),
        ("button", "middle", True),
        ("button", "middle", False),
        ("scroll", "up", 2),
    ]


def test_scroll_rejects_bad_direction_and_amount():
    module = load_module()
    with pytest.raises(ValueError, match="滚轮方向"):
        module.run({}, {"mode": "scroll", "direction": "left"})
    with pytest.raises(ValueError, match="滚轮格数"):
        module.run({}, {"mode": "scroll", "direction": "down", "amount": 0})


def test_accepts_integer_float_coords(monkeypatch):
    module = load_module()
    calls = []

    class FakeServices:
        def move_mouse(self, x, y, absolute=True):
            calls.append((x, y, absolute))

    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(module, "platform_services", FakeServices)
    result = module.run({}, {"mode": "move", "x": 10.0, "y": 20.0})
    assert result == {"mode": "move", "x": 10, "y": 20}
    assert calls == [(10, 20, True)]
