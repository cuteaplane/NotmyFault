"""清空剪贴板动作
Windows 为 ctypes 函数声明参数类型并持有 NATIVE_LOCK，Linux 使用 linux_support 通道
"""

import ctypes
import os

from notmyfault.native import NATIVE_LOCK


def run(action_info, params):
    if os.name != "nt":
        from notmyfault.platform.linux_support import set_clipboard_text

        set_clipboard_text("")
        print("[Action:clipboard_clear] 剪贴板已清空")
        return {"cleared": True}

    user32 = ctypes.windll.user32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool

    opened = False
    try:
        with NATIVE_LOCK:
            if not user32.OpenClipboard(None):
                raise RuntimeError("无法打开剪贴板（可能被其他程序占用）")
            opened = True
            if not user32.EmptyClipboard():
                raise RuntimeError("清空剪贴板失败")
    finally:
        if opened:
            with NATIVE_LOCK:
                user32.CloseClipboard()
    print("[Action:clipboard_clear] 剪贴板已清空")
    return {"cleared": True}
