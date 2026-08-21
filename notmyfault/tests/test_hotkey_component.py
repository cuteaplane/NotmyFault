"""hotkey 录制组件：按键采集、取消和超时"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="全局按键采集只支持 Windows"
)

from notmyfault.components.session import ComponentSession
from notmyfault.triggers.hotkey import component as hotkey_component


def fake_keys(down):
    def key_down(vk):
        return vk in down
    return key_down


class TestHotkeyCapture:
    def test_modifier_and_key_returned(self):
        session = ComponentSession("hotkey", "record", {})
        key_down = fake_keys({0x11, 0x10, 0x41})  # Ctrl+Shift+A
        result = hotkey_component._wait_for_hotkey_windows(session, 0.5, key_down)
        assert result["hotkey"] == "Ctrl+Shift+A"

    def test_escape_cancels(self):
        session = ComponentSession("hotkey", "record", {})
        key_down = fake_keys({0x1B})
        result = hotkey_component._wait_for_hotkey_windows(session, 0.5, key_down)
        assert result == {"cancelled": True}

    def test_timeout_returns_timed_out(self, monkeypatch):
        session = ComponentSession("hotkey", "record", {})
        monkeypatch.setattr(hotkey_component.time, "monotonic", lambda: 100.0)
        result = hotkey_component._wait_for_hotkey_windows(
            session, 0.0, fake_keys(set())
        )
        assert result == {"timed_out": True}

    def test_invoke_capture_returns_hotkey(self, monkeypatch):
        monkeypatch.setattr(
            hotkey_component,
            "_wait_for_hotkey_windows",
            lambda session, timeout: {"hotkey": "Ctrl+A"},
        )
        session = ComponentSession("hotkey", "record", {})
        result = hotkey_component.invoke(session, "capture", {"timeout_seconds": 10})
        assert result["ok"] is True
        assert result["data"]["hotkey"] == "Ctrl+A"

    def test_invoke_unknown_method_fails(self):
        session = ComponentSession("hotkey", "record", {})
        result = hotkey_component.invoke(session, "nope", {})
        assert result["ok"] is False
        assert "未知方法" in result["error"]
