"""Windows 原生函数签名初始化。"""

import sys
from types import SimpleNamespace

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 提供 user32")
def test_typed_user32_only_writes_signatures_once(monkeypatch):
    import notmyfault.native as native

    class FakeFunction:
        def __init__(self):
            object.__setattr__(self, "writes", 0)

        def __setattr__(self, name, value):
            if name in ("argtypes", "restype"):
                object.__setattr__(self, "writes", self.writes + 1)
            object.__setattr__(self, name, value)

    names = (
        "IsWindowVisible",
        "GetWindowTextLengthW",
        "GetWindowTextW",
        "EnumWindows",
        "GetWindowThreadProcessId",
        "GetForegroundWindow",
        "GetWindowLongW",
        "SetWindowPos",
        "ShowWindow",
        "SetForegroundWindow",
        "PostMessageW",
    )
    fake_user32 = SimpleNamespace(**{name: FakeFunction() for name in names})
    monkeypatch.setattr(native, "_TYPED_USER32", None)
    monkeypatch.setattr(
        native.ctypes,
        "windll",
        SimpleNamespace(user32=fake_user32),
    )

    first = native.typed_user32()
    writes = sum(getattr(fake_user32, name).writes for name in names)
    second = native.typed_user32()

    assert first is fake_user32
    assert second is fake_user32
    assert sum(getattr(fake_user32, name).writes for name in names) == writes
