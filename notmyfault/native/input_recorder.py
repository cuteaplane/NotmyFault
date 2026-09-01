"""Windows 全局鼠标和键盘录制。"""

import copy
import ctypes
import queue
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Callable

from notmyfault.native.mouse import per_monitor_dpi_context


WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_QUIT = 0x0012
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_F10 = 0x79

_CTRL_KEYS = {VK_CONTROL, VK_LCONTROL, VK_RCONTROL}
_SHIFT_KEYS = {VK_SHIFT, VK_LSHIFT, VK_RSHIFT}
_STOP_MODIFIERS = _CTRL_KEYS | _SHIFT_KEYS
_MOUSE_MESSAGES = {
    WM_LBUTTONDOWN: ("mouse_down", "left"),
    WM_LBUTTONUP: ("mouse_up", "left"),
    WM_RBUTTONDOWN: ("mouse_down", "right"),
    WM_RBUTTONUP: ("mouse_up", "right"),
    WM_MBUTTONDOWN: ("mouse_down", "middle"),
    WM_MBUTTONUP: ("mouse_up", "middle"),
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


def _signed_word(value: int) -> int:
    word = (int(value) >> 16) & 0xFFFF
    return word - 0x10000 if word & 0x8000 else word


class InputRecorder:
    """在独立消息线程中安装低级输入钩子。"""

    def __init__(
        self,
        mouse_resolver: Callable[[int, int], Any] | None = None,
        keyboard_window_resolver: Callable[[], Any] | None = None,
        resolve_timeout: float = 0.25,
        keyboard_password_resolver: Callable[[], Any] | None = None,
    ) -> None:
        self._mouse_resolver = mouse_resolver
        self._keyboard_window_resolver = keyboard_window_resolver
        self._keyboard_password_resolver = keyboard_password_resolver
        self._resolve_timeout = max(0.0, min(float(resolve_timeout), 0.5))
        self._events: list[dict] = []
        self._events_lock = threading.RLock()
        self._pressed_keys: set[int] = set()
        self._skipped_keys: set[int] = set()
        self._pressed_buttons: set[str] = set()
        self._last_move: tuple[float, int, int] | None = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._resolve_queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._resolver_thread: threading.Thread | None = None
        self._thread_id = 0
        self._error = ""
        self._started_at = 0.0
        self._started_tick_ms = 0
        self._stopped_at = 0.0

    @property
    def recording(self) -> bool:
        return bool(
            self._thread is not None
            and self._thread.is_alive()
            and not self._stop_event.is_set()
        )

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("全局输入录制只支持 Windows")
        if self._thread is not None:
            raise RuntimeError("输入录制已经启动")
        self._started_at = time.monotonic()
        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        self._started_tick_ms = int(kernel32.GetTickCount64())
        self._resolver_thread = threading.Thread(
            target=self._resolve_loop,
            name="NotmyFaultMacroResolver",
            daemon=True,
        )
        self._thread = threading.Thread(
            target=self._hook_loop,
            name="NotmyFaultMacroRecorder",
            daemon=True,
        )
        self._resolver_thread.start()
        self._thread.start()
        if not self._ready_event.wait(3):
            self.stop()
            raise RuntimeError("等待 Windows 输入钩子启动超时")
        if self._error:
            self.stop()
            raise RuntimeError(self._error)

    def stop(self) -> None:
        self._stop_event.set()
        thread_id = self._thread_id
        if thread_id and sys.platform == "win32":
            user32 = ctypes.windll.user32
            user32.PostThreadMessageW.argtypes = [
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._resolve_queue.put(None)
        if (
            self._resolver_thread is not None
            and self._resolver_thread is not threading.current_thread()
        ):
            self._resolver_thread.join(timeout=3)
        if not self._stopped_at:
            self._stopped_at = time.monotonic()

    def snapshot(self) -> dict:
        with self._events_lock:
            events = copy.deepcopy(self._events)
        now = self._stopped_at or time.monotonic()
        return {
            "recording": self.recording,
            "events": events,
            "event_count": len(events),
            "elapsed_seconds": max(0.0, now - self._started_at),
            "error": self._error,
        }

    def _message_timestamp(self, message_time: int) -> float:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        current_tick = int(kernel32.GetTickCount64())
        candidate = (current_tick & ~0xFFFFFFFF) | (int(message_time) & 0xFFFFFFFF)
        if candidate - current_tick > 0x80000000:
            candidate -= 0x100000000
        elif current_tick - candidate > 0x80000000:
            candidate += 0x100000000
        return self._started_at + (candidate - self._started_tick_ms) / 1000.0

    def _append(self, event: dict, message_time: int | None = None) -> dict:
        event["timestamp"] = (
            self._message_timestamp(message_time)
            if message_time is not None
            else time.monotonic()
        )
        with self._events_lock:
            self._events.append(event)
        return event

    def _resolve_mouse(self, event: dict) -> None:
        if self._mouse_resolver is None:
            return
        done = threading.Event()
        self._resolve_queue.put(("mouse", event, done))
        done.wait(self._resolve_timeout)

    def _resolve_keyboard_window(self, event: dict) -> None:
        if self._keyboard_window_resolver is None:
            return
        self._resolve_queue.put(("keyboard_window", event, None))

    def _resolve_keyboard_password(self) -> bool:
        if self._keyboard_password_resolver is None:
            return False
        result = {"value": True}
        done = threading.Event()
        self._resolve_queue.put(("keyboard_password", result, done))
        if not done.wait(self._resolve_timeout):
            return True
        return bool(result.get("value", True))

    def _resolve_loop(self) -> None:
        while True:
            task = self._resolve_queue.get()
            if task is None:
                return
            kind, event, done = task
            try:
                with per_monitor_dpi_context():
                    if kind == "mouse":
                        value = self._mouse_resolver(event["x"], event["y"])
                    elif kind == "keyboard_password":
                        value = bool(self._keyboard_password_resolver())
                    else:
                        value = self._keyboard_window_resolver()
            except Exception as exc:
                value = True if kind == "keyboard_password" else None
                error = str(exc)
            else:
                error = ""
            with self._events_lock:
                if kind == "mouse":
                    event["selector"] = value
                elif kind == "keyboard_password":
                    event["value"] = value
                elif isinstance(value, dict) and value:
                    event["window"] = value
                if error and kind == "mouse":
                    event["selector_error"] = error[:240]
                elif error and kind == "keyboard_window":
                    event["window_error"] = error[:240]
            if done is not None:
                done.set()

    def _trim_stop_modifiers(self) -> None:
        with self._events_lock:
            while self._events:
                event = self._events[-1]
                if (
                    event.get("kind") == "keyboard"
                    and event.get("event") == "down"
                    and event.get("vk") in _STOP_MODIFIERS
                ):
                    self._events.pop()
                    continue
                break

    def _keyboard_event(self, message: int, data: KBDLLHOOKSTRUCT) -> bool:
        if data.flags & LLKHF_INJECTED:
            return False
        state = "down" if message in (WM_KEYDOWN, WM_SYSKEYDOWN) else "up"
        vk = int(data.vkCode)
        if state == "down":
            self._pressed_keys.add(vk)
        else:
            self._pressed_keys.discard(vk)
        ctrl = bool(self._pressed_keys & _CTRL_KEYS)
        shift = bool(self._pressed_keys & _SHIFT_KEYS)
        if vk == VK_F10 and ctrl and shift:
            self._trim_stop_modifiers()
            self._stop_event.set()
            ctypes.windll.user32.PostQuitMessage(0)
            return True
        if state == "up" and vk in self._skipped_keys:
            self._skipped_keys.discard(vk)
            return False
        if self._resolve_keyboard_password():
            if state == "down":
                self._skipped_keys.add(vk)
            return False
        self._skipped_keys.discard(vk)
        with self._events_lock:
            starts_group = not self._events or self._events[-1].get("kind") != "keyboard"
        event = self._append({
            "kind": "keyboard",
            "event": state,
            "vk": vk,
            "scan_code": int(data.scanCode),
            "extended": bool(data.flags & LLKHF_EXTENDED),
        }, int(data.time))
        if starts_group:
            self._resolve_keyboard_window(event)
        return False

    def _mouse_event(self, message: int, data: MSLLHOOKSTRUCT) -> None:
        if data.flags & LLMHF_INJECTED:
            return
        x = int(data.pt.x)
        y = int(data.pt.y)
        mapped = _MOUSE_MESSAGES.get(message)
        if mapped is not None:
            event_name, button = mapped
            event = self._append({
                "kind": event_name,
                "button": button,
                "x": x,
                "y": y,
            }, int(data.time))
            if event_name == "mouse_down":
                self._pressed_buttons.add(button)
                if button == "left":
                    self._resolve_mouse(event)
            else:
                self._pressed_buttons.discard(button)
            return
        if message == WM_MOUSEWHEEL:
            self._append({
                "kind": "mouse_wheel",
                "x": x,
                "y": y,
                "delta": _signed_word(data.mouseData),
            }, int(data.time))
            return
        if message != WM_MOUSEMOVE or not self._pressed_buttons:
            return
        now = self._message_timestamp(int(data.time))
        if self._last_move is not None:
            last_time, last_x, last_y = self._last_move
            if now - last_time < 0.03 and abs(x - last_x) + abs(y - last_y) < 8:
                return
        self._last_move = (now, x, y)
        self._append(
            {"kind": "mouse_move", "x": x, "y": y},
            int(data.time),
        )

    def _hook_loop(self) -> None:
        keyboard_hook = None
        mouse_hook = None
        dpi_context = per_monitor_dpi_context()
        dpi_context.__enter__()
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hook_proc = ctypes.WINFUNCTYPE(
                wintypes.LPARAM,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            @hook_proc
            def keyboard_callback(code, message, pointer):
                swallow = False
                if code == HC_ACTION and message in (
                    WM_KEYDOWN,
                    WM_KEYUP,
                    WM_SYSKEYDOWN,
                    WM_SYSKEYUP,
                ):
                    data = ctypes.cast(
                        pointer, ctypes.POINTER(KBDLLHOOKSTRUCT)
                    ).contents
                    swallow = self._keyboard_event(int(message), data)
                if swallow:
                    return 1
                return user32.CallNextHookEx(None, code, message, pointer)

            @hook_proc
            def mouse_callback(code, message, pointer):
                if code == HC_ACTION:
                    data = ctypes.cast(
                        pointer, ctypes.POINTER(MSLLHOOKSTRUCT)
                    ).contents
                    self._mouse_event(int(message), data)
                return user32.CallNextHookEx(None, code, message, pointer)

            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int,
                hook_proc,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            ]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            user32.CallNextHookEx.argtypes = [
                wintypes.HHOOK,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.CallNextHookEx.restype = wintypes.LPARAM
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            module = kernel32.GetModuleHandleW(None)
            self._thread_id = int(kernel32.GetCurrentThreadId())
            keyboard_hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, keyboard_callback, module, 0
            )
            mouse_hook = user32.SetWindowsHookExW(
                WH_MOUSE_LL, mouse_callback, module, 0
            )
            if not keyboard_hook or not mouse_hook:
                raise ctypes.WinError()
            self._ready_event.set()
            message = wintypes.MSG()
            while not self._stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._error = f"Windows 输入录制失败: {exc}"
            self._ready_event.set()
        finally:
            if keyboard_hook:
                ctypes.windll.user32.UnhookWindowsHookEx(keyboard_hook)
            if mouse_hook:
                ctypes.windll.user32.UnhookWindowsHookEx(mouse_hook)
            self._stop_event.set()
            self._stopped_at = time.monotonic()
            self._ready_event.set()
            dpi_context.__exit__(None, None, None)


def build_macro_steps(
    events: list[dict],
    started_at: float,
    screen: dict,
) -> list[dict]:
    """把低级输入事件整理成可编辑的宏步骤。"""
    ordered = sorted(
        (event for event in events if isinstance(event, dict)),
        key=lambda event: float(event.get("timestamp", 0)),
    )
    steps: list[dict] = []
    last_time = float(
        started_at
        if started_at is not None
        else (ordered[0].get("timestamp", 0) if ordered else 0)
    )
    pending: dict[str, dict] = {}

    def point(event: dict) -> dict:
        return {
            "version": 1,
            "x": int(event["x"]),
            "y": int(event["y"]),
            "screen": copy.deepcopy(screen),
        }

    def emit(event_time: float, step: dict, end_time: float | None = None) -> None:
        nonlocal last_time
        step["delay_seconds"] = round(max(0.0, event_time - last_time), 3)
        steps.append(step)
        last_time = max(event_time, end_time if end_time is not None else event_time)

    index = 0
    while index < len(ordered):
        event = ordered[index]
        kind = event.get("kind")
        event_time = float(event.get("timestamp", last_time))
        if kind == "keyboard":
            for button, down in pending.items():
                if down.get("emitted"):
                    continue
                emit(
                    float(down.get("timestamp", event_time)),
                    {
                        "kind": "coordinate",
                        "point": point(down),
                        "operation": f"{button}_down",
                    },
                )
                down["emitted"] = True
            group = []
            window = copy.deepcopy(event.get("window"))
            group_start = event_time
            previous = event_time
            while index < len(ordered) and ordered[index].get("kind") == "keyboard":
                current = ordered[index]
                current_time = float(current.get("timestamp", previous))
                group.append({
                    "event": current.get("event"),
                    "vk": current.get("vk"),
                    "scan_code": current.get("scan_code", 0),
                    "extended": bool(current.get("extended")),
                    "delay_seconds": round(max(0.0, current_time - previous), 3),
                })
                previous = current_time
                index += 1
            step = {"kind": "keyboard", "events": group}
            if isinstance(window, dict) and window:
                step["window"] = window
            emit(group_start, step, previous)
            continue
        if kind == "mouse_down":
            pending[event.get("button", "left")] = event
        elif kind == "mouse_move":
            for button, down in list(pending.items()):
                if not down.get("emitted"):
                    emit(
                        float(down.get("timestamp", event_time)),
                        {
                            "kind": "coordinate",
                            "point": point(down),
                            "operation": f"{button}_down",
                        },
                    )
                    down["emitted"] = True
            emit(
                event_time,
                {"kind": "coordinate", "point": point(event), "operation": "move"},
            )
        elif kind == "mouse_up":
            button = event.get("button", "left")
            down = pending.pop(button, None)
            if down is None:
                emit(
                    event_time,
                    {
                        "kind": "coordinate",
                        "point": point(event),
                        "operation": f"{button}_up",
                    },
                )
            elif down.get("emitted"):
                emit(
                    event_time,
                    {
                        "kind": "coordinate",
                        "point": point(event),
                        "operation": f"{button}_up",
                    },
                )
            else:
                down_time = float(down.get("timestamp", event_time))
                selector = down.get("selector")
                if button == "left" and isinstance(selector, dict) and selector:
                    step = {
                        "kind": "control",
                        "selector": copy.deepcopy(selector),
                        "operation": "invoke",
                        "text": "",
                        "wait_seconds": 30,
                    }
                else:
                    step = {
                        "kind": "coordinate",
                        "point": point(down),
                        "operation": f"{button}_click",
                    }
                emit(down_time, step, event_time)
        elif kind == "mouse_wheel":
            emit(
                event_time,
                {
                    "kind": "coordinate",
                    "point": point(event),
                    "operation": "scroll",
                    "mouse_data": int(event.get("delta", 0)),
                },
            )
        index += 1
    for button, down in pending.items():
        emit(
            float(down.get("timestamp", last_time)),
            {
                "kind": "coordinate",
                "point": point(down),
                "operation": f"{button}_down",
            },
        )
    return steps
