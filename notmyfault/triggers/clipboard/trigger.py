import time
import ctypes

CF_TEXT = 1
GMEM_MOVEABLE = 0x0002


def _get_clipboard_text():
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_TEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            text = ctypes.c_char_p(ptr).value
            return text.decode("utf-8", errors="replace") if text else None
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def run(meta, config_list, emit_event, shutdown_event):
    trigger_id = meta.get("id", "clipboard")
    print(f"[Trigger:{trigger_id}] 剪贴板监控启动")

    match_texts = set()
    any_match = False
    for cfg in config_list:
        t = cfg.get("match_text", "").strip()
        if t:
            match_texts.add(t)
        else:
            any_match = True

    last_content = _get_clipboard_text()

    while not shutdown_event.is_set():
        try:
            current = _get_clipboard_text()
            if current is not None and current != last_content:
                if any_match:
                    print(f"[Trigger:{trigger_id}] 剪贴板内容变化")
                    emit_event(trigger_id, {"text": current[:200]})
                else:
                    for mt in match_texts:
                        if mt in current:
                            print(f"[Trigger:{trigger_id}] 剪贴板匹配: {mt}")
                            emit_event(trigger_id, {"text": current[:200], "matched": mt})
                            break
                last_content = current
        except Exception as e:
            print(f"[Trigger:{trigger_id}] 检查剪贴板出错: {e}")

        shutdown_event.wait(1)
