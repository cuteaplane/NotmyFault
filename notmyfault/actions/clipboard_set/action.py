import ctypes

GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
# 64 位兼容：返回类型和参数类型都要显式声明
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p


def run(action_info, params):
    text = params.get("text", "")

    if not text:
        print("[Action:clipboard_set] 没有文本可写入")
        return

    print(f"[Action:clipboard_set] 写入剪贴板: {text[:50]}...")

    clipboard_open = False
    handle = None
    transferred = False
    try:
        if not user32.OpenClipboard(None):
            print("[Action:clipboard_set] 无法打开剪贴板")
            return
        clipboard_open = True

        user32.EmptyClipboard()

        # 使用 CF_UNICODETEXT (UTF-16) 以支持中文等非 ASCII 字符
        buf = ctypes.create_unicode_buffer(text)
        byte_len = len(buf) * ctypes.sizeof(ctypes.c_wchar)

        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, byte_len)
        if not handle:
            print("[Action:clipboard_set] GlobalAlloc 失败")
            return

        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            print("[Action:clipboard_set] GlobalLock 失败")
            return
        ctypes.memmove(ptr, buf, byte_len)
        kernel32.GlobalUnlock(handle)

        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            print("[Action:clipboard_set] SetClipboardData 失败")
            return
        transferred = True  # 成功后句柄所有权转交 Windows，不能再 GlobalFree。
        print(f"[Action:clipboard_set] 剪贴板写入成功 ({len(text)} 字符)")

    except Exception as e:
        print(f"[Action:clipboard_set] 写入失败: {e}")
    finally:
        if handle and not transferred:
            kernel32.GlobalFree(handle)
        if clipboard_open:
            user32.CloseClipboard()
