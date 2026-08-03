import ctypes
import os

from notmyfault.triggers.base import PollingTrigger

CF_UNICODETEXT = 13

if os.name == "nt":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # ctypes 在多线程下共享 _objects 引用表，函数声明需要完整
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool


def _get_clipboard_text():
    if os.name != "nt":
        from notmyfault.platform.linux_support import get_clipboard_text
        return get_clipboard_text()
    # NATIVE_LOCK 保护 ctypes 调用，共享引用表在多线程下存在竞态
    from notmyfault.native import NATIVE_LOCK
    with NATIVE_LOCK:
        return _get_clipboard_text_locked()


def _get_clipboard_text_locked():
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
# CF_UNICODETEXT 以 NUL 结尾，传入 GlobalSize 会把终止符也读入
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


class ClipboardTrigger(PollingTrigger):
    """剪贴板内容监控，match_text 为空时任意内容变化都会触发"""

    interval = 1.0
    native = True

    def setup(self):
        self.match_text = str(self.config.get("match_text", "")).strip()
        self._last_content = _get_clipboard_text()
        self.log("剪贴板监控启动")

    def poll(self):
        current = _get_clipboard_text()
        if current is None or current == self._last_content:
            return
        if not self.match_text:
            self.log("剪贴板内容变化")
            self.emit({"text": current[:200], "match_text": ""})
        else:
            current_lower = current.lower()
            if self.match_text.lower() in current_lower:
                self.log(f"剪贴板匹配: {self.match_text}")
                self.emit({
                    "text": current[:200],
                    "match_text": self.match_text,
                    "matched": self.match_text,
                })
        self._last_content = current


def run(meta, config, emit_event, shutdown_event):
    ClipboardTrigger(meta, config, emit_event, shutdown_event).run()
