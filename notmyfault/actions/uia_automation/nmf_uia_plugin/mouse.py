"""Windows 屏幕坐标采集与鼠标输入。"""

import ctypes
import sys
from contextlib import contextmanager
from ctypes import wintypes

from .native_support import NATIVE_LOCK


INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

_OPERATIONS = {
    "left_click": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "double_click": (
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
    ),
    "right_click": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle_click": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    "left_down": (MOUSEEVENTF_LEFTDOWN,),
    "left_up": (MOUSEEVENTF_LEFTUP,),
    "right_down": (MOUSEEVENTF_RIGHTDOWN,),
    "right_up": (MOUSEEVENTF_RIGHTUP,),
    "middle_down": (MOUSEEVENTF_MIDDLEDOWN,),
    "middle_up": (MOUSEEVENTF_MIDDLEUP,),
    "scroll": (MOUSEEVENTF_WHEEL,),
    "move": (),
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("屏幕坐标鼠标操作只支持 Windows")


def _user32():
    _require_windows()
    return ctypes.windll.user32


@contextmanager
def per_monitor_dpi_context():
    user32 = _user32()
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    if setter is None:
        yield
        return
    setter.argtypes = [ctypes.c_void_p]
    setter.restype = ctypes.c_void_p
    previous = setter(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    try:
        yield
    finally:
        if previous:
            setter(previous)


def _virtual_screen() -> dict:
    user32 = _user32()
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    return {
        "left": int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        "top": int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        "width": int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        "height": int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    }


def current_virtual_screen() -> dict:
    """返回当前 Windows 虚拟桌面范围。"""
    with NATIVE_LOCK:
        with per_monitor_dpi_context():
            return _virtual_screen()


def _point_coordinates(point) -> tuple[int, int]:
    if not isinstance(point, dict):
        raise ValueError("屏幕坐标格式无效")
    x = point.get("x")
    y = point.get("y")
    if isinstance(x, bool) or not isinstance(x, int):
        raise ValueError("屏幕横坐标必须是整数")
    if isinstance(y, bool) or not isinstance(y, int):
        raise ValueError("屏幕纵坐标必须是整数")
    return x, y


def _inside_screen(x: int, y: int, screen: dict) -> bool:
    return (
        screen["width"] > 0
        and screen["height"] > 0
        and screen["left"] <= x < screen["left"] + screen["width"]
        and screen["top"] <= y < screen["top"] + screen["height"]
    )


def capture_cursor_position() -> dict:
    """记录鼠标在 Windows 虚拟桌面上的绝对坐标。"""
    with NATIVE_LOCK:
        with per_monitor_dpi_context():
            user32 = _user32()
            user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
            user32.GetCursorPos.restype = wintypes.BOOL
            position = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(position)):
                raise RuntimeError("无法读取当前鼠标位置")
            return {
                "version": 1,
                "x": int(position.x),
                "y": int(position.y),
                "screen": _virtual_screen(),
            }


def check_coordinate(point) -> dict:
    """检查绝对坐标是否仍位于当前虚拟桌面内。"""
    x, y = _point_coordinates(point)
    with NATIVE_LOCK:
        with per_monitor_dpi_context():
            screen = _virtual_screen()
    found = _inside_screen(x, y, screen)
    result = {"found": found, "x": x, "y": y, "screen": screen}
    if not found:
        result["error"] = "这个坐标已不在当前屏幕范围内，请检查显示器布局"
    return result


def _mouse_input(flags: int, mouse_data: int = 0) -> INPUT:
    value = INPUT()
    value.type = INPUT_MOUSE
    value.mi.mouseData = mouse_data & 0xFFFFFFFF
    value.mi.dwFlags = flags
    return value


def _send_mouse_flags(flags: tuple[int, ...], mouse_data: int = 0) -> None:
    if not flags:
        return
    inputs = (INPUT * len(flags))(*(
        _mouse_input(flag, mouse_data if flag == MOUSEEVENTF_WHEEL else 0)
        for flag in flags
    ))
    user32 = _user32()
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    sent = user32.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise RuntimeError(f"SendInput 只发送了 {sent}/{len(inputs)} 个鼠标事件")


def perform_coordinate(
    point,
    operation="left_click",
    cancellation=None,
    mouse_data: int = 0,
) -> dict:
    """移动到绝对坐标并执行一次鼠标操作。"""
    x, y = _point_coordinates(point)
    if operation not in _OPERATIONS:
        raise ValueError(f"不支持的坐标操作: {operation}")
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    with NATIVE_LOCK:
        with per_monitor_dpi_context():
            screen = _virtual_screen()
            if not _inside_screen(x, y, screen):
                raise ValueError("宏记录的坐标已不在当前屏幕范围内，请检查显示器布局")
            user32 = _user32()
            user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
            user32.SetCursorPos.restype = wintypes.BOOL
            if not user32.SetCursorPos(x, y):
                raise RuntimeError(f"无法把鼠标移动到屏幕坐标 ({x}, {y})")
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            _send_mouse_flags(_OPERATIONS[operation], mouse_data)
    return {
        "kind": "coordinate",
        "operation": operation,
        "x": x,
        "y": y,
        **({"mouse_data": mouse_data} if operation == "scroll" else {}),
    }
