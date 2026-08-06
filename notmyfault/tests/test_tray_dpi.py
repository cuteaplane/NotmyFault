"""托盘线程 Per-Monitor V2 DPI 上下文设置的单元测试。"""

import ctypes

from notmyfault.host import tray


class _FakeContextFunc:
    """记录 argtypes/restype 设置和调用参数的假 Win32 函数。"""

    def __init__(self):
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, context):
        self.calls.append(context)
        return ctypes.c_void_p(-5)


class _FakeUser32:
    def __init__(self):
        self.SetThreadDpiAwarenessContext = _FakeContextFunc()


def test_enable_tray_dpi_awareness_uses_per_monitor_v2(monkeypatch):
    fake_user32 = _FakeUser32()
    monkeypatch.setattr(
        tray.ctypes, "WinDLL", lambda name, use_last_error=False: fake_user32
    )

    assert tray._enable_tray_dpi_awareness() is True

    func = fake_user32.SetThreadDpiAwarenessContext
    assert func.argtypes == [ctypes.c_void_p]
    assert func.restype is ctypes.c_void_p
    assert len(func.calls) == 1
    assert func.calls[0] is tray._DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2


def test_enable_tray_dpi_awareness_tolerates_older_windows(monkeypatch):
    # 老系统没有该 API：加载失败抛 OSError，或函数属性缺失抛 AttributeError
    def win_dll_missing(_name, use_last_error=False):
        raise OSError("user32 不可用")

    monkeypatch.setattr(tray.ctypes, "WinDLL", win_dll_missing)
    assert tray._enable_tray_dpi_awareness() is False

    monkeypatch.setattr(tray.ctypes, "WinDLL", lambda *a, **kw: object())
    assert tray._enable_tray_dpi_awareness() is False
