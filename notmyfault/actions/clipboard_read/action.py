import ctypes
import os

from notmyfault.plugin_api import native_lock, platform_services


def _read_windows(max_chars):
    with native_lock():
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = ctypes.c_bool
        user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_bool
        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        if not user32.OpenClipboard(None):
            raise RuntimeError("无法打开剪贴板，可能被其他程序占用")
        try:
            if not user32.IsClipboardFormatAvailable(13):
                return None
            handle = user32.GetClipboardData(13)
            if not handle:
                raise RuntimeError("无法读取剪贴板文本")
            size = kernel32.GlobalSize(handle)
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise RuntimeError("无法锁定剪贴板文本")
            try:
                # UTF-16 的代理对占两个码元，额外读取空间用于判断字符是否截断。
                raw = ctypes.string_at(pointer, min(size, (max_chars + 1) * 4))
                text = raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]
                return text
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    max_chars = int(params.get("max_chars", 1000000))
    if not 1 <= max_chars <= 1000000:
        raise ValueError("最多读取字符数必须在 1 到 1000000 之间")
    text = (
        _read_windows(max_chars)
        if os.name == "nt"
        else platform_services().read_clipboard()
    )
    value = text or ""
    return {
        "text": value[:max_chars],
        "length": len(value[:max_chars]),
        "has_text": text is not None,
        "truncated": len(value) > max_chars,
    }
