from __future__ import annotations

import ctypes

from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError
from notmyfault.platform.windows import _win32

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


class ClipboardBackend:
    def __init__(self, runner=None) -> None:
        self._runner = runner

    def read(self, max_chars: int | None = None) -> str | None:
        if max_chars is not None and (
            isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0
        ):
            raise BackendInvalidError("max_chars 必须是正整数")
        with NATIVE_LOCK:
            user32 = _win32.user32()
            kernel32 = _win32.kernel32()
            if not user32.OpenClipboard(None):
                raise BackendFailedError("无法打开剪贴板，可能被其他程序占用")
            try:
                if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
                    return None
                handle = user32.GetClipboardData(_CF_UNICODETEXT)
                if not handle:
                    raise BackendFailedError("无法读取剪贴板文本")
                size = kernel32.GlobalSize(handle)
                if not size:
                    raise BackendFailedError("无法读取剪贴板文本大小")
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    raise BackendFailedError("无法锁定剪贴板文本")
                try:
                    # UTF-16 的代理对占两个码元。
                    byte_count = size if max_chars is None else min(size, max_chars * 4)
                    raw = ctypes.string_at(pointer, byte_count)
                    text = raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]
                    return text if max_chars is None else text[:max_chars]
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()

    def write(self, text: str) -> None:
        if not isinstance(text, str):
            raise BackendInvalidError("剪贴板内容必须是文本")
        data = text.encode("utf-16-le", errors="surrogatepass") + b"\0\0"
        with NATIVE_LOCK:
            user32 = _win32.user32()
            kernel32 = _win32.kernel32()
            if not user32.OpenClipboard(None):
                raise BackendFailedError("无法打开剪贴板，可能被其他程序占用")
            handle = None
            transferred = False
            try:
                if not user32.EmptyClipboard():
                    raise BackendFailedError("清空剪贴板失败")
                handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
                if not handle:
                    raise BackendFailedError("GlobalAlloc 分配内存失败")
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    raise BackendFailedError("GlobalLock 失败")
                try:
                    ctypes.memmove(pointer, data, len(data))
                finally:
                    kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
                    raise BackendFailedError("SetClipboardData 失败")
                # 成功后句柄所有权转交 Windows。
                transferred = True
            finally:
                if handle and not transferred:
                    kernel32.GlobalFree(handle)
                user32.CloseClipboard()

    def clear(self) -> None:
        with NATIVE_LOCK:
            user32 = _win32.user32()
            if not user32.OpenClipboard(None):
                raise BackendFailedError("无法打开剪贴板，可能被其他程序占用")
            try:
                if not user32.EmptyClipboard():
                    raise BackendFailedError("清空剪贴板失败")
            finally:
                user32.CloseClipboard()
