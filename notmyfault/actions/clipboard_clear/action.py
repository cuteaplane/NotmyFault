"""清空剪贴板动作
Windows 为 ctypes 函数声明参数类型并持有 NATIVE_LOCK，Linux 使用平台服务
"""

import ctypes
import os

from notmyfault.plugin_api import native_lock, platform_services

NATIVE_LOCK = native_lock()


def run(action_info, params):
    if os.name != "nt":
        platform_services().write_clipboard("")
        print("[Action:clipboard_clear] 剪贴板已清空")
        return {"cleared": True}

    user32 = ctypes.windll.user32
    opened = False
    with NATIVE_LOCK:
        try:
            user32.OpenClipboard.argtypes = [ctypes.c_void_p]
            user32.OpenClipboard.restype = ctypes.c_int
            user32.EmptyClipboard.argtypes = []
            user32.EmptyClipboard.restype = ctypes.c_int
            user32.CloseClipboard.argtypes = []
            user32.CloseClipboard.restype = ctypes.c_int
            if not user32.OpenClipboard(None):
                raise RuntimeError("无法打开剪贴板（可能被其他程序占用）")
            opened = True
            if not user32.EmptyClipboard():
                raise RuntimeError("清空剪贴板失败")
        finally:
            if opened:
                user32.CloseClipboard()
    print("[Action:clipboard_clear] 剪贴板已清空")
    return {"cleared": True}
