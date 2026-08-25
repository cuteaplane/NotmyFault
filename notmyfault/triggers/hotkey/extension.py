import os
import time


def capture_hotkey(context, payload):
    options = payload if isinstance(payload, dict) else {}
    try:
        timeout = float(options.get("timeout_seconds", 15))
    except (TypeError, ValueError):
        timeout = 15.0
    timeout = min(max(timeout, 3.0), 60.0)
    if os.name == "nt":
        result = _wait_for_hotkey_windows(context, timeout)
    else:
        result = _wait_for_hotkey_linux(context, timeout)
    if "hotkey" in result:
        return context.commit_value(result["hotkey"])
    if result.get("error"):
        return context.error(result["error"], close=True)
    if result.get("timed_out"):
        return context.error("没有等到按键，请再试一次", close=True)
    return context.result(result, close=True)


def _key_down(vk: int) -> bool:
    import ctypes

    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _wait_for_hotkey_windows(context, timeout: float, key_down=None) -> dict:
    from notmyfault.triggers.hotkey.trigger import _VK_MAP

    if key_down is None:
        key_down = _key_down
    vk_escape = 0x1B
    vk_modifiers = [("CTRL", 0x11), ("ALT", 0x12), ("SHIFT", 0x10), ("WIN", 0x5B)]
    vk_names = {vk: name for name, vk in _VK_MAP.items()}
    pretty = {"CTRL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift", "WIN": "Win"}

    deadline = time.monotonic() + timeout
    context.session.set_status("请按下要录制的快捷键，按 Esc 取消…")
    while time.monotonic() < deadline:
        if key_down(vk_escape):
            return {"cancelled": True}
        modifiers = [name for name, vk in vk_modifiers if key_down(vk)]
        for vk, name in vk_names.items():
            if key_down(vk):
                return {"hotkey": "+".join([*(pretty[mod] for mod in modifiers), name])}
        time.sleep(0.05)
    return {"timed_out": True}


def _wait_for_hotkey_linux(context, timeout: float) -> dict:
    try:
        from Xlib import X, XK
        from Xlib.display import Display
    except ImportError:
        return {"error": "Linux 热键录制需要 python-xlib，请运行 pip install python-xlib"}

    modifier_masks = ((4, "Ctrl"), (8, "Alt"), (1, "Shift"), (64, "Super"))
    display = Display()
    root = display.screen().root
    root.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
    display.sync()
    deadline = time.monotonic() + timeout
    context.session.set_status("请按下要录制的快捷键，按 Esc 取消…")
    result = {}

    try:
        while time.monotonic() < deadline:
            if display.pending_events() == 0:
                time.sleep(0.02)
                continue
            event = display.next_event()
            if event.type != X.KeyPress:
                continue
            keysym = display.keycode_to_keysym(event.detail, 0)
            if keysym == 0xFF1B:
                result["cancelled"] = True
                break
            name = XK.keysym_to_string(keysym) or ""
            if not name or name.lower() in {
                "control_l", "control_r", "shift_l", "shift_r", "alt_l", "alt_r",
                "super_l", "super_r",
            }:
                continue
            display_name = name.upper() if len(name) == 1 else name
            display_name = {
                "return": "Enter", "enter": "Enter", "escape": "Esc", "esc": "Esc",
                "space": "Space", "tab": "Tab", "backspace": "BackSpace",
            }.get(name.lower(), display_name)
            modifiers = [
                label for mask, label in modifier_masks if event.state & mask
            ]
            result["hotkey"] = "+".join([*modifiers, display_name])
            break
    finally:
        try:
            root.ungrab_keyboard(X.CurrentTime)
            display.sync()
        except Exception:
            pass
        try:
            display.close()
        except Exception:
            pass

    return result or {"timed_out": True}
