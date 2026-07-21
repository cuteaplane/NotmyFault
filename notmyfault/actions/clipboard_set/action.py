import ctypes

GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13


def run(action_info, params):
    text = params.get("text", "")

    if not text:
        print("[Action:clipboard_set] 没有文本可写入")
        return

    print(f"[Action:clipboard_set] 写入剪贴板: {text[:50]}...")

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    try:
        if not user32.OpenClipboard(None):
            print("[Action:clipboard_set] 无法打开剪贴板")
            return

        user32.EmptyClipboard()

        # 使用 CF_UNICODETEXT (UTF-16) 以支持中文等非 ASCII 字符
        data = text + "\x00"
        buf = ctypes.create_unicode_buffer(data)
        byte_len = len(buf) * ctypes.sizeof(ctypes.c_wchar)

        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, byte_len)
        if not handle:
            print("[Action:clipboard_set] GlobalAlloc 失败")
            user32.CloseClipboard()
            return

        ptr = kernel32.GlobalLock(handle)
        if ptr:
            ctypes.memmove(ptr, buf, byte_len)
            kernel32.GlobalUnlock(handle)

        user32.SetClipboardData(CF_UNICODETEXT, handle)
        user32.CloseClipboard()
        print(f"[Action:clipboard_set] 剪贴板写入成功 ({len(text)} 字符)")

    except Exception as e:
        print(f"[Action:clipboard_set] 写入失败: {e}")
        try:
            user32.CloseClipboard()
        except Exception:
            pass
