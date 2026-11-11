from __future__ import annotations

import ctypes

from notmyfault.platform._keys import parse_hotkey
from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError
from notmyfault.platform.windows import _win32

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_MODIFIERS = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "super": 0x5B}
_SPECIAL_KEYS = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "escape": 0x1B, "esc": 0x1B,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "insert": 0x2D,
    "minus": 0xBD, "equal": 0xBB, "comma": 0xBC, "dot": 0xBE,
    "slash": 0xBF, "semicolon": 0xBA, "apostrophe": 0xDE,
    "grave": 0xC0, "backslash": 0xDC, "leftbrace": 0xDB, "rightbrace": 0xDD,
}


def _key_input(vk: int, scan: int, flags: int):
    inp = _win32.INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = scan
    inp.ki.dwFlags = flags
    return inp


def _send(inputs: list) -> None:
    if not inputs:
        return
    array = (_win32.INPUT * len(inputs))(*inputs)
    user32 = _win32.user32()
    sent = user32.SendInput(len(array), array, ctypes.sizeof(_win32.INPUT))
    if sent != len(array):
        pressed = {}
        for item in inputs[:sent]:
            key = (
                item.ki.wVk,
                item.ki.wScan,
                item.ki.dwFlags & ~_KEYEVENTF_KEYUP,
            )
            if item.ki.dwFlags & _KEYEVENTF_KEYUP:
                pressed.pop(key, None)
            else:
                pressed[key] = None
        releases = [
            _key_input(vk, scan, flags | _KEYEVENTF_KEYUP)
            for vk, scan, flags in reversed(pressed)
        ]
        message = f"SendInput 只发送了 {sent}/{len(array)} 个输入事件"
        if releases:
            cleanup = (_win32.INPUT * len(releases))(*releases)
            released = user32.SendInput(len(cleanup), cleanup, ctypes.sizeof(_win32.INPUT))
            if released != len(cleanup):
                message += "，部分按键未能释放"
        raise BackendFailedError(message)


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


def _virtual_key(key: str) -> int:
    if key in _MODIFIERS:
        return _MODIFIERS[key]
    if key in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[key]
    if key.startswith("f") and key[1:].isdigit():
        return 0x70 + int(key[1:]) - 1
    return ord(key.upper())


class KeyboardBackend:
    def __init__(self, runner=None) -> None:
        self._runner = runner

    def type_text(self, text: str) -> None:
        if not isinstance(text, str) or not text:
            raise BackendInvalidError("没有要输入的文本")
        with NATIVE_LOCK:
            inputs = []
            for code in _utf16_code_units(text):
                inputs.append(_key_input(0, code, _KEYEVENTF_UNICODE))
                inputs.append(_key_input(0, code, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
            _send(inputs)

    def send_hotkey(self, keys: list[str]) -> None:
        parts = parse_hotkey(keys)
        with NATIVE_LOCK:
            vk_parts = [_virtual_key(part) for part in parts]
            main_vk = vk_parts.pop()
            inputs = [_key_input(vk, 0, 0) for vk in vk_parts]
            inputs.append(_key_input(main_vk, 0, 0))
            inputs.append(_key_input(main_vk, 0, _KEYEVENTF_KEYUP))
            inputs.extend(_key_input(vk, 0, _KEYEVENTF_KEYUP) for vk in reversed(vk_parts))
            _send(inputs)
