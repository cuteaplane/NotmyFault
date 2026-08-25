"""hotkey 参数编辑器：按键采集、取消和超时"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="全局按键采集只支持 Windows"
)

from notmyfault.extensions.session import ExtensionContext, ExtensionSession
from notmyfault.triggers.hotkey import extension as hotkey_extension


def fake_keys(down):
    def key_down(vk):
        return vk in down

    return key_down


class TestHotkeyCapture:
    @staticmethod
    def context():
        session = ExtensionSession(
            plugin_id="hotkey",
            command_id="capture_hotkey",
            plugin_meta={"package_name": "io.github.notmyfault.hotkey"},
            source_kind="parameter_editors",
            source_id="hotkey_recorder",
            allowed_commands={"capture_hotkey"},
            data_type=None,
            current_value="",
            value_type="string",
        )
        return ExtensionContext(session, None)

    def test_modifier_and_key_returned(self):
        key_down = fake_keys({0x11, 0x10, 0x41})
        result = hotkey_extension._wait_for_hotkey_windows(
            self.context(), 0.5, key_down
        )
        assert result["hotkey"] == "Ctrl+Shift+A"

    def test_escape_cancels(self):
        key_down = fake_keys({0x1B})
        result = hotkey_extension._wait_for_hotkey_windows(
            self.context(), 0.5, key_down
        )
        assert result == {"cancelled": True}

    def test_timeout_returns_timed_out(self, monkeypatch):
        monkeypatch.setattr(hotkey_extension.time, "monotonic", lambda: 100.0)
        result = hotkey_extension._wait_for_hotkey_windows(
            self.context(), 0.0, fake_keys(set())
        )
        assert result == {"timed_out": True}

    def test_command_commits_captured_hotkey(self, monkeypatch):
        monkeypatch.setattr(
            hotkey_extension,
            "_wait_for_hotkey_windows",
            lambda context, timeout: {"hotkey": "Ctrl+A"},
        )
        result = hotkey_extension.capture_hotkey(
            self.context(), {"timeout_seconds": 10}
        )
        assert result["ok"] is True
        assert result["value"] == "Ctrl+A"
        assert result["close"] is True

    def test_command_reports_timeout(self, monkeypatch):
        monkeypatch.setattr(
            hotkey_extension,
            "_wait_for_hotkey_windows",
            lambda context, timeout: {"timed_out": True},
        )
        result = hotkey_extension.capture_hotkey(self.context(), {})
        assert result == {
            "ok": False,
            "error": "没有等到按键，请再试一次",
            "close": True,
        }
