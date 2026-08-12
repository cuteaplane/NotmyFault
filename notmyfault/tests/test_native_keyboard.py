"""Windows 键盘事件校验与回放。"""

from types import SimpleNamespace

import pytest

import notmyfault.native.keyboard as keyboard


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


def test_validate_key_event():
    assert keyboard.validate_key_event({
        "event": "up", "vk": 65, "scan_code": 30, "extended": True,
    }) == (65, 30, True, True)


def test_input_structure_matches_windows_abi():
    expected = 40 if keyboard.ctypes.sizeof(keyboard.ctypes.c_void_p) == 8 else 28
    assert keyboard.ctypes.sizeof(keyboard.INPUT) == expected


@pytest.mark.parametrize("event", [
    None,
    {"event": "down", "vk": True},
    {"event": "down", "vk": 300},
    {"event": "other", "vk": 65},
    {"event": "down", "vk": 65, "scan_code": -1},
])
def test_validate_key_event_rejects_invalid_data(event):
    with pytest.raises(ValueError):
        keyboard.validate_key_event(event)


def test_perform_key_event_uses_recorded_scan_code(monkeypatch):
    captured = []

    def send_input(count, pointer, size):
        value = pointer._obj
        captured.append((count, value.type, value.ki.wScan, value.ki.dwFlags, size))
        return 1

    user32 = SimpleNamespace(SendInput=FakeFunction(send_input))
    monkeypatch.setattr(keyboard.sys, "platform", "win32")
    monkeypatch.setattr(keyboard.ctypes, "windll", SimpleNamespace(user32=user32))
    result = keyboard.perform_key_event({
        "event": "up", "vk": 65, "scan_code": 30, "extended": True,
    })
    assert captured[0][1:4] == (
        keyboard.INPUT_KEYBOARD,
        30,
        keyboard.KEYEVENTF_SCANCODE
        | keyboard.KEYEVENTF_KEYUP
        | keyboard.KEYEVENTF_EXTENDEDKEY,
    )
    assert result["event"] == "up"
