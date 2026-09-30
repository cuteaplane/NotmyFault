"""鼠标操作：移动光标、单击双击、按下释放，或滚动滚轮
Windows 用 SetCursorPos / SendInput；Linux 走 InputBackend（xdotool 或 ydotool）
"""

from __future__ import annotations

import os
from typing import Any

from notmyfault.plugin_api import native_lock, platform_services

NATIVE_LOCK = native_lock()

_MOVE_MODES = frozenset({"move", "move_relative"})
_CLICK_MODES = frozenset({"click", "double_click", "press", "release"})
_SCROLL_MODES = frozenset({"scroll"})
_ALL_MODES = _MOVE_MODES | _CLICK_MODES | _SCROLL_MODES
_BUTTONS = frozenset({"left", "right", "middle"})
_SCROLL_DIRECTIONS = frozenset({"up", "down"})
_WHEEL_DELTA = 120


def _require_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        # Dashboard number 参数可能是 float
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"{name} 必须是整数")
    return value


def _normalize_button(raw) -> str:
    button = str(raw or "left").strip().lower()
    if button not in _BUTTONS:
        raise ValueError(f"不支持的鼠标按键: {raw!r}（可选: left/right/middle）")
    return button


def _normalize_direction(raw) -> str:
    direction = str(raw or "down").strip().lower()
    if direction not in _SCROLL_DIRECTIONS:
        raise ValueError(f"不支持的滚轮方向: {raw!r}（可选: up/down）")
    return direction


def _linux_move(x: int, y: int, *, absolute: bool) -> None:
    platform_services().move_mouse(x, y, absolute)


def _linux_click(button: str, clicks: int) -> None:
    platform_services().click_mouse(button, clicks)


def _linux_button(button: str, pressed: bool) -> None:
    platform_services().mouse_button(button, pressed)


def _linux_scroll(direction: str, amount: int) -> None:
    platform_services().scroll_mouse(direction, amount)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    INPUT_MOUSE = 0
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_WHEEL = 0x0800

    _DOWN = {
        "left": MOUSEEVENTF_LEFTDOWN,
        "right": MOUSEEVENTF_RIGHTDOWN,
        "middle": MOUSEEVENTF_MIDDLEDOWN,
    }
    _UP = {
        "left": MOUSEEVENTF_LEFTUP,
        "right": MOUSEEVENTF_RIGHTUP,
        "middle": MOUSEEVENTF_MIDDLEUP,
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

    def _mouse_input(flags: int, mouse_data: int = 0) -> INPUT:
        value = INPUT()
        value.type = INPUT_MOUSE
        value.mi.dwFlags = flags
        value.mi.mouseData = mouse_data & 0xFFFFFFFF
        return value

    def _send_mouse_flags(flags: list[int], mouse_data: int = 0) -> None:
        if not flags:
            return
        array = (INPUT * len(flags))(*(
            _mouse_input(flag, mouse_data if flag == MOUSEEVENTF_WHEEL else 0)
            for flag in flags
        ))
        user32 = ctypes.windll.user32
        user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        ]
        user32.SendInput.restype = wintypes.UINT
        sent = user32.SendInput(len(array), array, ctypes.sizeof(INPUT))
        if sent != len(array):
            pressed = set()
            pairs = {
                MOUSEEVENTF_LEFTDOWN: MOUSEEVENTF_LEFTUP,
                MOUSEEVENTF_RIGHTDOWN: MOUSEEVENTF_RIGHTUP,
                MOUSEEVENTF_MIDDLEDOWN: MOUSEEVENTF_MIDDLEUP,
            }
            for flag in flags[:sent]:
                if flag in pairs:
                    pressed.add(pairs[flag])
                else:
                    pressed.discard(flag)
            message = f"SendInput 只发送了 {sent}/{len(array)} 个鼠标事件"
            if pressed:
                releases = (INPUT * len(pressed))(
                    *(_mouse_input(flag) for flag in sorted(pressed))
                )
                if user32.SendInput(
                    len(releases), releases, ctypes.sizeof(INPUT)
                ) != len(releases):
                    message += "，部分鼠标按键未能释放"
            raise RuntimeError(message)

    def _cursor_pos() -> tuple[int, int]:
        user32 = ctypes.windll.user32
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            raise RuntimeError("无法读取当前鼠标位置")
        return int(point.x), int(point.y)

    def _set_cursor_pos(x: int, y: int) -> None:
        user32 = ctypes.windll.user32
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        if not user32.SetCursorPos(x, y):
            raise RuntimeError(f"无法把鼠标移动到屏幕坐标 ({x}, {y})")

    def _windows_move(x: int, y: int, *, absolute: bool) -> None:
        with NATIVE_LOCK:
            if absolute:
                _set_cursor_pos(x, y)
            else:
                current_x, current_y = _cursor_pos()
                _set_cursor_pos(current_x + x, current_y + y)

    def _windows_click(button: str, clicks: int) -> None:
        flags: list[int] = []
        for _ in range(clicks):
            flags.append(_DOWN[button])
            flags.append(_UP[button])
        with NATIVE_LOCK:
            _send_mouse_flags(flags)

    def _windows_button(button: str, pressed: bool) -> None:
        flag = _DOWN[button] if pressed else _UP[button]
        with NATIVE_LOCK:
            _send_mouse_flags([flag])

    def _windows_scroll(direction: str, amount: int) -> None:
        # WHEEL_DELTA=120，正数远离用户（向上），负数靠近用户（向下）
        delta = _WHEEL_DELTA * amount
        if direction == "down":
            delta = -delta
        with NATIVE_LOCK:
            _send_mouse_flags([MOUSEEVENTF_WHEEL], mouse_data=delta)


def run(action_info, params):
    mode = str(params.get("mode", "move") or "move").strip().lower()
    if mode not in _ALL_MODES:
        raise ValueError(
            f"未知模式: {mode!r}（可选: move/move_relative/click/double_click/press/release/scroll）"
        )

    result: dict[str, Any] = {"mode": mode}

    if mode == "move":
        x = _require_int("横坐标", params.get("x", 0))
        y = _require_int("纵坐标", params.get("y", 0))
        if os.name == "nt":
            _windows_move(x, y, absolute=True)
        else:
            _linux_move(x, y, absolute=True)
        result["x"] = x
        result["y"] = y
    elif mode == "move_relative":
        dx = _require_int("水平偏移", params.get("dx", 0))
        dy = _require_int("垂直偏移", params.get("dy", 0))
        if os.name == "nt":
            _windows_move(dx, dy, absolute=False)
        else:
            _linux_move(dx, dy, absolute=False)
        result["x"] = dx
        result["y"] = dy
    elif mode == "scroll":
        direction = _normalize_direction(params.get("direction", "down"))
        amount = _require_int("滚轮格数", params.get("amount", 3))
        if amount < 1:
            raise ValueError("滚轮格数必须是不小于 1 的整数")
        if os.name == "nt":
            _windows_scroll(direction, amount)
        else:
            _linux_scroll(direction, amount)
        result["direction"] = direction
        result["amount"] = amount
    else:
        button = _normalize_button(params.get("button", "left"))
        result["button"] = button
        if mode == "click":
            if os.name == "nt":
                _windows_click(button, 1)
            else:
                _linux_click(button, 1)
        elif mode == "double_click":
            if os.name == "nt":
                _windows_click(button, 2)
            else:
                _linux_click(button, 2)
        elif mode == "press":
            if os.name == "nt":
                _windows_button(button, True)
            else:
                _linux_button(button, True)
        else:
            if os.name == "nt":
                _windows_button(button, False)
            else:
                _linux_button(button, False)

    if mode == "scroll":
        detail = f" direction={result.get('direction')} amount={result.get('amount')}"
    elif "button" in result:
        detail = f" button={result.get('button')}"
    else:
        detail = f" x={result.get('x')} y={result.get('y')}"
    print(f"[Action:mouse] 已执行: mode={mode}{detail}")
    return result
