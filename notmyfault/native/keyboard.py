"""Windows 键盘扫描码回放。"""

import ctypes
import sys
from ctypes import wintypes

from notmyfault.native import NATIVE_LOCK


INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def validate_key_event(event) -> tuple[int, int, bool, bool]:
    if not isinstance(event, dict):
        raise ValueError("键盘事件格式无效")
    vk = event.get("vk")
    scan_code = event.get("scan_code", 0)
    if isinstance(vk, bool) or not isinstance(vk, int) or not 0 <= vk <= 0xFF:
        raise ValueError("键盘事件缺少有效虚拟键码")
    if (
        isinstance(scan_code, bool)
        or not isinstance(scan_code, int)
        or not 0 <= scan_code <= 0xFFFF
    ):
        raise ValueError("键盘事件扫描码无效")
    state = event.get("event")
    if state not in ("down", "up"):
        raise ValueError("键盘事件必须是 down 或 up")
    return vk, scan_code, state == "up", bool(event.get("extended"))


def perform_key_event(event, cancellation=None) -> dict:
    """使用录制的虚拟键码和扫描码发送一次按键事件。"""
    if sys.platform != "win32":
        raise RuntimeError("键盘宏回放只支持 Windows")
    vk, scan_code, key_up, extended = validate_key_event(event)
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    flags = KEYEVENTF_KEYUP if key_up else 0
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    value = INPUT()
    value.type = INPUT_KEYBOARD
    if scan_code:
        value.ki.wScan = scan_code
        value.ki.dwFlags = flags | KEYEVENTF_SCANCODE
    else:
        value.ki.wVk = vk
        value.ki.dwFlags = flags
    with NATIVE_LOCK:
        user32 = ctypes.windll.user32
        user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        ]
        user32.SendInput.restype = wintypes.UINT
        sent = user32.SendInput(1, ctypes.byref(value), ctypes.sizeof(INPUT))
    if sent != 1:
        raise RuntimeError("SendInput 没有发送键盘事件")
    return {
        "kind": "keyboard",
        "event": "up" if key_up else "down",
        "vk": vk,
        "scan_code": scan_code,
    }
