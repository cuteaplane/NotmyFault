"""插件内部共用的 Windows 原生函数声明。"""

import ctypes
import sys
from ctypes import wintypes

from notmyfault.plugin_api import native_lock


NATIVE_LOCK = native_lock()
_TYPED_USER32 = None


def typed_user32():
    """返回只由本插件维护签名的 user32 函数表。"""
    if sys.platform != "win32":
        raise RuntimeError("UIA 自动化插件仅支持 Windows")
    global _TYPED_USER32
    with NATIVE_LOCK:
        if _TYPED_USER32 is not None:
            return _TYPED_USER32
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        _TYPED_USER32 = user32
        return user32
