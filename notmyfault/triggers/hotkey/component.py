"""hotkey 插件的录制组件：等待按键后返回对应的热键字符串"""

import os
import time


def describe() -> dict:
    return {"methods": ["capture"]}


def invoke(session, method, payload):
    options = payload if isinstance(payload, dict) else {}
    if method == "capture":
        try:
            timeout = float(options.get("timeout_seconds", 15))
        except (TypeError, ValueError):
            timeout = 15.0
        timeout = min(max(timeout, 3.0), 60.0)
        if os.name == "nt":
            return {"ok": True, "data": _wait_for_hotkey_windows(session, timeout)}
        return {"ok": True, "data": _wait_for_hotkey_linux(session, timeout)}
    return {"ok": False, "error": f"未知方法: {method}"}


def _key_down(vk: int) -> bool:
    import ctypes
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _wait_for_hotkey_windows(session, timeout: float, key_down=None) -> dict:
    from notmyfault.triggers.hotkey.trigger import _VK_MAP

    if key_down is None:
        key_down = _key_down
    VK_ESCAPE = 0x1B
    VK_MODS = [("CTRL", 0x11), ("ALT", 0x12), ("SHIFT", 0x10), ("WIN", 0x5B)]
    VK_NAMES = {vk: name for name, vk in _VK_MAP.items()}
    PRETTY = {"CTRL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift", "WIN": "Win"}

    deadline = time.monotonic() + timeout
    session.set_status("请按下要录制的快捷键，按 Esc 取消…")
    while time.monotonic() < deadline:
        if key_down(VK_ESCAPE):
            return {"cancelled": True}
        mods = [name for name, vk in VK_MODS if key_down(vk)]
        for vk, name in VK_NAMES.items():
            if key_down(vk):
                pretty_mods = [PRETTY.get(mod, mod) for mod in mods]
                return {"hotkey": "+".join([*pretty_mods, name])}
        time.sleep(0.05)
    return {"timed_out": True}


def _wait_for_hotkey_linux(session, timeout: float) -> dict:
    try:
        from Xlib import X
        from Xlib.display import Display
    except ImportError:
        return {"error": "Linux 热键录制需要 python-xlib，请运行 pip install python-xlib"}

    _MOD_MASKS = {4: "Ctrl", 1: "Shift", 8: "Alt", 64: "Super"}

    disp = Display()
    root = disp.screen().root
    # 监听所有按键事件
    root.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
    disp.sync()

    deadline = time.monotonic() + timeout
    session.set_status("请按下要录制的快捷键，按 Esc 取消…")
    result = {}

    while time.monotonic() < deadline:
        n = disp.pending_events()
        if n == 0:
            time.sleep(0.02)
            continue
        ev = disp.next_event()
        if ev.type != X.KeyPress:
            continue
        keycode = ev.detail
        keysym = disp.keycode_to_keysym(keycode, 0)
        state = ev.state

        # Esc 取消
        if keysym == 0xFF1B:
            result["cancelled"] = True
            break

        # 从 keysym 查名称
        name = disp.lookup_string(keysym) or ""
        if not name:
            name_str = disp.keysym_to_string(keysym) or ""
            name = name_str
        if not name:
            continue

        # 忽略纯修饰键按下
        if name.lower() in ("control_l", "control_r", "shift_l", "shift_r",
                             "alt_l", "alt_r", "super_l", "super_r"):
            continue

        # 收集修饰键
        parts = []
        for mask, label in sorted(_MOD_MASKS.items(), key=lambda x: x[0]):
            if state & mask:
                parts.append(label)
        # 按键名美化
        display_name = name.upper() if len(name) == 1 else name
        if name.lower() in ("return", "enter"):
            display_name = "Enter"
        elif name.lower() in ("escape", "esc"):
            display_name = "Esc"
        elif name.lower() == "space":
            display_name = "Space"
        elif name.lower() == "tab":
            display_name = "Tab"
        elif name.lower() == "backspace":
            display_name = "BackSpace"
        parts.append(display_name)
        result["hotkey"] = "+".join(parts)
        break

    # 释放键盘抓取
    try:
        root.ungrab_keyboard(X.CurrentTime)
        disp.sync()
    except Exception:
        pass

    if not result:
        return {"timed_out": True}
    return result
