"""hotkey 插件的录制组件：等待按键后返回对应的热键字符串"""

import ctypes
import time

from notmyfault.triggers.hotkey.trigger import _VK_MAP

VK_ESCAPE = 0x1B
_VK_MODS = [("CTRL", 0x11), ("ALT", 0x12), ("SHIFT", 0x10), ("WIN", 0x5B)]
_VK_NAMES = {vk: name for name, vk in _VK_MAP.items()}
_PRETTY = {
    "CTRL": "Ctrl",
    "ALT": "Alt",
    "SHIFT": "Shift",
    "WIN": "Win",
}


def describe() -> dict:
    """组件支持的调用方法，Dashboard 按这份清单渲染采集入口"""
    return {
        "methods": ["capture"],
    }


def _key_down(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _wait_for_hotkey(session, timeout: float, key_down=None) -> dict:
    """轮询修饰键和主键，返回热键、取消或超时结果"""
    key_down = key_down or _key_down
    deadline = time.monotonic() + timeout
    session.set_status("请按下要录制的快捷键，按 Esc 取消…")
    while time.monotonic() < deadline:
        if key_down(VK_ESCAPE):
            return {"cancelled": True}
        mods = [name for name, vk in _VK_MODS if key_down(vk)]
        for vk, name in _VK_NAMES.items():
            if key_down(vk):
                pretty_mods = [_PRETTY.get(mod, mod) for mod in mods]
                return {"hotkey": "+".join([*pretty_mods, name])}
        time.sleep(0.05)
    return {"timed_out": True}


def invoke(session, method, payload):
    options = payload if isinstance(payload, dict) else {}
    if method == "capture":
        try:
            timeout = float(options.get("timeout_seconds", 15))
        except (TypeError, ValueError):
            timeout = 15.0
        timeout = min(max(timeout, 3.0), 60.0)
        return {"ok": True, "data": _wait_for_hotkey(session, timeout)}
    return {"ok": False, "error": f"未知方法: {method}"}
