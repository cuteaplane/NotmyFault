import time
import ctypes
from ctypes import wintypes


def _get_window_titles() -> dict:
    """枚举所有可见窗口，返回 {hwnd: title} 字典"""
    user32 = ctypes.windll.user32
    # 显式声明参数/返回类型：无 argtypes 的并发调用曾在与 clipboard 触发器
    # 同时运行时造成堆损坏（0xc0000374）。
    if not getattr(user32, "_nmf_typed", False):
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32._nmf_typed = True
    titles = {}

    # 原生段互斥：多线程并发 ctypes 曾与 clipboard 组合触发堆损坏
    from notmyfault._native_guard import NATIVE_LOCK
    with NATIVE_LOCK:
        return _get_window_titles_locked(user32)


def _get_window_titles_locked(user32) -> dict:
    titles = {}

    def enum_callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if title.strip():
                    titles[hwnd] = title
        return True  # 继续枚举

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    return titles


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "window_title")
    pattern = config.get("title_pattern", "").strip().lower()
    if not pattern:
        raise ValueError("未配置标题关键词（title_pattern 为空）")

    print(f"[Trigger:{trigger_id}] 开始监视窗口标题: {pattern}")
    target_state = config.get("state", "opened")
    if target_state not in ("opened", "closed"):
        raise ValueError(
            f"无效的窗口状态: {target_state!r}（可选: opened/closed）"
        )
    was_matched = False

    while not shutdown_event.is_set():
        try:
            titles = _get_window_titles()
            # 找出实际命中的标题（可能多个窗口同时命中）
            matched_titles = [t for t in titles.values() if pattern in t.lower()]
            matched = bool(matched_titles)

            if matched and not was_matched:
                if target_state == "opened":
                    actual_title = max(matched_titles, key=len)
                    print(f"[Trigger:{trigger_id}] 窗口出现: '{pattern}'")
                    emit_event({
                        "title_pattern": pattern,
                        "state": "opened",
                        "matched_title": actual_title,
                    })
                was_matched = True
            elif not matched and was_matched:
                if target_state == "closed":
                    print(f"[Trigger:{trigger_id}] 窗口关闭: '{pattern}'")
                    emit_event({
                        "title_pattern": pattern,
                        "state": "closed",
                        "matched_title": "",
                    })
                was_matched = False

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 扫描出错: {e}")

        shutdown_event.wait(3)
