import time
import ctypes
from ctypes import wintypes


def _get_window_titles() -> dict:
    """枚举所有可见窗口，返回 {hwnd: title} 字典"""
    result = {}
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


def run(meta, config_list, emit_event, shutdown_event):
    trigger_id = meta.get("id", "window_title")

    patterns = {}
    for cfg in config_list:
        pattern = cfg.get("title_pattern", "").strip().lower()
        expected_state = cfg.get("state", "opened")
        if pattern:
            patterns[pattern] = expected_state

    if not patterns:
        print(f"[Trigger:{trigger_id}] 未配置标题关键词，退出")
        return

    print(f"[Trigger:{trigger_id}] 开始监视窗口标题: {list(patterns.keys())}")

    # 记录每个模式当前是否已匹配到
    was_matched = {p: False for p in patterns}

    while not shutdown_event.is_set():
        try:
            titles = _get_window_titles()
            all_text = " ".join(titles.values()).lower()

            for pattern, expected_state in patterns.items():
                matched = pattern in all_text

                if matched and not was_matched[pattern]:
                    print(f"[Trigger:{trigger_id}] 窗口出现: '{pattern}'")
                    emit_event(trigger_id, {
                        "title_pattern": pattern,
                        "state": "opened",
                        "matched_title": pattern
                    })
                    was_matched[pattern] = True
                elif not matched and was_matched[pattern]:
                    print(f"[Trigger:{trigger_id}] 窗口关闭: '{pattern}'")
                    emit_event(trigger_id, {
                        "title_pattern": pattern,
                        "state": "closed",
                        "matched_title": pattern
                    })
                    was_matched[pattern] = False

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 扫描出错: {e}")

        time.sleep(3)
