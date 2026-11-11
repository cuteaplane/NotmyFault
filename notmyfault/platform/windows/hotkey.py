from __future__ import annotations

import ctypes
import math
import threading
import time

from notmyfault.platform._keys import SPECIAL_KEYS, parse_hotkey
from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform._run import default_runner
from notmyfault.platform.errors import BackendError, BackendFailedError, BackendInvalidError
from notmyfault.platform.windows import _win32
from notmyfault.platform.windows.keyboard import _virtual_key

_LISTENERS = {}
_next_id = 1


def _drain(user32):
    message = _win32.MSG()
    thread_id = threading.get_ident()
    while user32.PeekMessageW(ctypes.byref(message), None, _win32.WM_HOTKEY,
                             _win32.WM_HOTKEY, _win32.PM_REMOVE):
        listener = _LISTENERS.get(int(message.wParam))
        if message.message == _win32.WM_HOTKEY and listener is not None and listener._thread == thread_id:
            listener._pending += 1


class _Listener:
    def __init__(self, user32, hotkey_id):
        self._user32 = user32
        self._id = hotkey_id
        self._thread = threading.get_ident()
        self._pending = 0
        self._closed = False

    def poll(self) -> int:
        with NATIVE_LOCK:
            if self._closed:
                return 0
            if threading.get_ident() != self._thread:
                raise BackendFailedError("热键必须在注册线程轮询")
            try:
                _drain(self._user32)
            except Exception as error:
                raise BackendFailedError(f"读取 Windows 热键失败: {error}") from error
            count = self._pending
            self._pending = 0
            return count

    def close(self) -> None:
        with NATIVE_LOCK:
            if self._closed:
                return
            if threading.get_ident() != self._thread:
                raise BackendFailedError("热键必须在注册线程关闭")
            self._closed = True
            _LISTENERS.pop(self._id, None)
            try:
                if not self._user32.UnregisterHotKey(None, self._id):
                    raise BackendFailedError("注销 Windows 热键失败")
            except BackendError:
                raise
            except Exception as error:
                raise BackendFailedError(f"注销 Windows 热键失败: {error}") from error


class HotkeyBackend:
    def __init__(self, runner=None) -> None:
        self._runner = runner or default_runner

    def register(self, keys: list[str]):
        parts = parse_hotkey(keys)
        if len(parts) < 2:
            raise BackendInvalidError("全局热键必须包含修饰键")
        global _next_id
        try:
            with NATIVE_LOCK:
                for _ in range(0xBFFF):
                    hotkey_id = _next_id
                    _next_id = _next_id % 0xBFFF + 1
                    if hotkey_id not in _LISTENERS:
                        break
                else:
                    raise BackendFailedError("Windows 热键注册数量已达上限")
                mod_map = {
                    "ctrl": _win32.MOD_CONTROL, "alt": _win32.MOD_ALT,
                    "shift": _win32.MOD_SHIFT, "super": _win32.MOD_WIN,
                }
                modifiers = _win32.MOD_NOREPEAT
                for key in parts[:-1]:
                    modifiers |= mod_map[key]
                user32 = _win32.user32()
                if not user32.RegisterHotKey(None, hotkey_id, modifiers, _virtual_key(parts[-1])):
                    raise BackendFailedError("热键注册失败，组合键可能已被其他规则或程序占用")
                listener = _Listener(user32, hotkey_id)
                _LISTENERS[hotkey_id] = listener
                return listener
        except BackendError:
            raise
        except Exception as error:
            raise BackendFailedError(f"注册 Windows 热键失败: {error}") from error

    def capture(self, timeout: float) -> list[str]:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError, OverflowError) as error:
            raise BackendInvalidError("热键录制超时必须是有限非负数") from error
        if not math.isfinite(timeout) or timeout < 0:
            raise BackendInvalidError("热键录制超时必须是有限非负数")
        if timeout == 0:
            return []
        candidates = sorted({
            parse_hotkey([key])[-1]
            for key in {*SPECIAL_KEYS, *"abcdefghijklmnopqrstuvwxyz0123456789"}
        } - {"escape", "capslock"})
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                with NATIVE_LOCK:
                    user32 = _win32.user32()
                    if user32.GetAsyncKeyState(_virtual_key("escape")) & 0x8000:
                        return []
                    modifiers = [
                        key for key in ("ctrl", "alt", "shift", "super")
                        if user32.GetAsyncKeyState(_virtual_key(key)) & 0x8000
                        or (key == "super" and user32.GetAsyncKeyState(_win32.VK_RWIN) & 0x8000)
                    ]
                    if modifiers:
                        pressed = [key for key in candidates if user32.GetAsyncKeyState(_virtual_key(key)) & 0x8000]
                        if len(pressed) == 1:
                            return parse_hotkey([*modifiers, pressed[0]])
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            return []
        except BackendError:
            raise
        except Exception as error:
            raise BackendFailedError(f"录制 Windows 热键失败: {error}") from error
