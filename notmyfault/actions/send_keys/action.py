"""发送按键动作：向当前活动窗口输入文本或发送组合键
Windows 用 SendInput；Linux 用 xdotool 或 ydotool
"""

import os
import shutil
import subprocess

from notmyfault.native import NATIVE_LOCK

_LINUX_MODIFIERS = {"ctrl", "shift", "alt", "super", "win"}
_LINUX_SPECIAL_KEYS = {
    "enter": "Return", "return": "Return", "tab": "Tab", "space": "space",
    "backspace": "BackSpace", "delete": "Delete", "escape": "Escape", "esc": "Escape",
    "home": "Home", "end": "End", "pgup": "Page_Up", "pgdn": "Page_Down",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "capslock": "Caps_Lock", "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
    "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10",
    "f11": "F11", "f12": "F12",
}


def _find_input_tool() -> str | None:
    return shutil.which("xdotool") or shutil.which("ydotool")


def _linux_type_text(text: str) -> None:
    tool = _find_input_tool()
    if not tool:
        raise RuntimeError("缺少输入后端，请安装 xdotool 或 ydotool")
    tool_name = os.path.basename(tool)
    if tool_name == "xdotool":
        subprocess.run(
            [tool, "type", "--clearmodifiers", "--", text],
            check=True, timeout=30,
        )
    else:
        subprocess.run(
            [tool, "type", "--", text],
            check=True, timeout=30,
        )


def _linux_send_hotkey(keys: str) -> None:
    parts = [part.strip().lower() for part in keys.replace("+", " ").split() if part.strip()]
    if not parts:
        raise ValueError("热键不能为空")
    main_key_seen = False
    for part in parts:
        if part in _LINUX_MODIFIERS:
            if main_key_seen:
                raise ValueError(f"组合键中只允许修饰键在前: {keys!r}")
        else:
            if main_key_seen:
                raise ValueError(f"组合键只能包含一个主键: {keys!r}")
            main_key_seen = True
    tool = _find_input_tool()
    if not tool:
        raise RuntimeError("缺少输入后端，请安装 xdotool 或 ydotool")
    tool_name = os.path.basename(tool)
    if tool_name == "xdotool":
        xdo_parts = []
        for part in parts:
            if part in _LINUX_SPECIAL_KEYS:
                xdo_parts.append(_LINUX_SPECIAL_KEYS[part])
            elif part in _LINUX_MODIFIERS:
                xdo_parts.append(part.replace("win", "super"))
            elif len(part) == 1:
                xdo_parts.append(part)
            else:
                xdo_parts.append(part)
        combo = "+".join(xdo_parts)
        subprocess.run(
            [tool, "key", "--clearmodifiers", combo],
            check=True, timeout=10,
        )
    else:
        ydo_parts = []
        for part in parts:
            if part in _LINUX_SPECIAL_KEYS:
                ydo_parts.append(_LINUX_SPECIAL_KEYS[part])
            elif part in _LINUX_MODIFIERS:
                ydo_parts.append(part.replace("win", "super"))
            elif len(part) == 1:
                ydo_parts.append(part)
            else:
                ydo_parts.append(part)
        combo = "+".join(ydo_parts)
        subprocess.run(
            [tool, "key", combo],
            check=True, timeout=10,
        )


if os.name == "nt":
    import ctypes
    import time
    from ctypes import wintypes

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004

    _MODIFIERS = {
        "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    }
    _SPECIAL_KEYS = {
        "enter": 0x0D, "return": 0x0D, "tab": 0x09, "space": 0x20,
        "backspace": 0x08, "delete": 0x2E, "escape": 0x1B, "esc": 0x1B,
        "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "capslock": 0x14,
    }

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [
            ("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    def _key_input(vk: int, scan: int, flags: int) -> INPUT:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki.wVk = vk
        inp.ki.wScan = scan
        inp.ki.dwFlags = flags
        return inp

    def _send(inputs: list[INPUT]) -> None:
        if not inputs:
            return
        array = (INPUT * len(inputs))(*inputs)
        user32 = ctypes.windll.user32
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        sent = user32.SendInput(len(array), array, ctypes.sizeof(INPUT))
        if sent != len(array):
            time.sleep(0.05)
            sent = user32.SendInput(len(array), array, ctypes.sizeof(INPUT))
            if sent != len(array):
                raise RuntimeError(f"SendInput 只发送了 {sent}/{len(array)} 个输入事件")

    def _utf16_code_units(text: str) -> list[int]:
        units = []
        for ch in text:
            code = ord(ch)
            if code <= 0xFFFF:
                units.append(code)
                continue
            code -= 0x10000
            units.append(0xD800 + (code >> 10))
            units.append(0xDC00 + (code & 0x3FF))
        return units

    def _type_unicode(text: str) -> None:
        inputs = []
        for code in _utf16_code_units(text):
            inputs.append(_key_input(0, code, KEYEVENTF_UNICODE))
            inputs.append(_key_input(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        _send(inputs)

    def _parse_vk(token: str) -> int:
        token = token.strip().lower()
        if token in _MODIFIERS:
            return _MODIFIERS[token]
        if token in _SPECIAL_KEYS:
            return _SPECIAL_KEYS[token]
        if token.startswith("f") and token[1:].isdigit():
            n = int(token[1:])
            if 1 <= n <= 24:
                return 0x70 + (n - 1)
        if len(token) == 1:
            return ord(token.upper()) if "a" <= token <= "z" else ord(token)
        raise ValueError(f"无法识别的按键: {token!r}")

    def _send_hotkey(keys: str) -> None:
        parts = [part for part in keys.replace("+", " ").split() if part]
        if not parts:
            raise ValueError("热键不能为空")
        vk_parts = [_parse_vk(part) for part in parts]
        main_vk = vk_parts.pop()
        modifier_vks = set(_MODIFIERS.values())
        modifiers = []
        for vk in vk_parts:
            if vk not in modifier_vks:
                raise ValueError(f"组合键中只允许修饰键在前: {keys!r}")
            modifiers.append(vk)
        inputs = []
        for vk in modifiers:
            inputs.append(_key_input(vk, 0, 0))
        inputs.append(_key_input(main_vk, 0, 0))
        inputs.append(_key_input(main_vk, 0, KEYEVENTF_KEYUP))
        for vk in reversed(modifiers):
            inputs.append(_key_input(vk, 0, KEYEVENTF_KEYUP))
        _send(inputs)


def run(action_info, params):
    mode = params.get("mode", "type_text")
    if os.name == "nt":
        with NATIVE_LOCK:
            if mode == "type_text":
                text = str(params.get("text", "") or "")
                if not text:
                    raise ValueError("没有要输入的文本")
                _type_unicode(text)
            elif mode == "hotkey":
                keys = str(params.get("keys", "") or "")
                _send_hotkey(keys)
            else:
                raise ValueError(f"未知模式: {mode!r}（可选: type_text/hotkey）")
    else:
        if mode == "type_text":
            text = str(params.get("text", "") or "")
            if not text:
                raise ValueError("没有要输入的文本")
            _linux_type_text(text)
        elif mode == "hotkey":
            keys = str(params.get("keys", "") or "")
            _linux_send_hotkey(keys)
        else:
            raise ValueError(f"未知模式: {mode!r}（可选: type_text/hotkey）")
    print(f"[Action:send_keys] 已发送: mode={mode}")
    return {"mode": mode}
