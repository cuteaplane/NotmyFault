import ctypes
import os

from notmyfault.plugin_api import native_lock

NATIVE_LOCK = native_lock()

GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13

if os.name == "nt":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
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
        raise ValueError("没有文本可写入")

    print(f"[Action:clipboard_set] 准备写入剪贴板 ({len(text)} 字符)")

    if os.name != "nt":
        from notmyfault.platform.linux_support import set_clipboard_text

        set_clipboard_text(str(text))
        print(f"[Action:clipboard_set] 剪贴板写入成功 ({len(text)} 字符)")
        return

    clipboard_open = False
    handle = None
    transferred = False
    # 剪贴板 API 也是共享 user32 函数对象，与项目其他 ctypes 调用一样持锁
    try:
        with NATIVE_LOCK:
            if not user32.OpenClipboard(None):
                raise RuntimeError("无法打开剪贴板（可能被其他程序占用）")
            clipboard_open = True

            if not user32.EmptyClipboard():
                raise RuntimeError("清空剪贴板失败")

            # 使用 CF_UNICODETEXT 的 UTF-16 编码以支持中文等非 ASCII 字符
            buf = ctypes.create_unicode_buffer(text)
            byte_len = len(buf) * ctypes.sizeof(ctypes.c_wchar)

            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, byte_len)
            if not handle:
                raise RuntimeError("GlobalAlloc 分配内存失败")

            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                raise RuntimeError("GlobalLock 失败")
            ctypes.memmove(ptr, buf, byte_len)
            kernel32.GlobalUnlock(handle)

            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise RuntimeError("SetClipboardData 失败")
            transferred = True  # 成功后句柄所有权转交 Windows，不能再 GlobalFree
            print(f"[Action:clipboard_set] 剪贴板写入成功 ({len(text)} 字符)")
    finally:
        with NATIVE_LOCK:
            if handle and not transferred:
                kernel32.GlobalFree(handle)
            if clipboard_open:
                user32.CloseClipboard()
