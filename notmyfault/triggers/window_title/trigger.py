import time
import ctypes
from ctypes import wintypes


def _get_window_titles() -> dict:
    """枚举所有可见窗口，返回 {hwnd: title} 字典"""
    user32 = ctypes.windll.user32
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
        print(f"[Trigger:{trigger_id}] 未配置标题关键词，退出")
        return

    print(f"[Trigger:{trigger_id}] 开始监视窗口标题: {pattern}")
    target_state = config.get("state", "opened")
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
