import ctypes
import os

CF_UNICODETEXT = 13

if os.name == "nt":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]


def _get_clipboard_text():
    if os.name != "nt":
        from notmyfault.linux_support import get_clipboard_text
        return get_clipboard_text()
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
            # CF_UNICODETEXT 保证以 NUL 结尾；传入 GlobalSize 会把终止符也读入。
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "clipboard")
    print(f"[Trigger:{trigger_id}] 剪贴板监控启动")

    match_text = config.get("match_text", "").strip()

    last_content = _get_clipboard_text()

    while not shutdown_event.is_set():
        try:
            current = _get_clipboard_text()
            if current is not None and current != last_content:
                if not match_text:
                    print(f"[Trigger:{trigger_id}] 剪贴板内容变化")
                    emit_event({"text": current[:200], "match_text": ""})
                else:
                    current_lower = current.lower()
                    if match_text.lower() in current_lower:
                        print(f"[Trigger:{trigger_id}] 剪贴板匹配: {match_text}")
                        emit_event({
                            "text": current[:200],
                            "match_text": match_text,
                            "matched": match_text,
                        })
                last_content = current
        except Exception as e:
            print(f"[Trigger:{trigger_id}] 检查剪贴板出错: {e}")

        shutdown_event.wait(1)
