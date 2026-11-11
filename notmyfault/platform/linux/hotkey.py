from __future__ import annotations

import math
import time

from notmyfault.platform._keys import parse_hotkey
from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform._run import default_runner
from notmyfault.platform.errors import (
    BackendError, BackendFailedError, BackendInvalidError, BackendMissingError,
)
from notmyfault.platform.linux.keyboard import _SPECIAL_KEYS

_MODIFIERS = {"ctrl": 4, "alt": 8, "shift": 1, "super": 64}


def _xlib():
    try:
        from Xlib import X, XK
        from Xlib.display import Display
    except ImportError as error:
        raise BackendMissingError("全局热键需要 python-xlib") from error
    return X, XK, Display


def _close_display(display, release):
    failure = None
    with NATIVE_LOCK:
        try:
            release()
            display.sync()
        except Exception as error:
            failure = error
        finally:
            try:
                display.close()
            except Exception as error:
                failure = failure or error
    if failure is not None:
        raise BackendFailedError(f"释放 X11 热键资源失败: {failure}") from failure


class _Listener:
    def __init__(self, display, root, keycode, masks, x):
        self._display = display
        self._root = root
        self._keycode = keycode
        self._masks = masks
        self._x = x
        self._closed = False
        self._release = None

    def poll(self) -> int:
        with NATIVE_LOCK:
            if self._closed:
                return 0
            count = 0
            try:
                while self._display.pending_events():
                    event = self._display.next_event()
                    stamp = (getattr(event, "detail", None), getattr(event, "time", None))
                    if event.type == self._x.KeyRelease:
                        self._release = stamp
                        continue
                    repeated = self._release == stamp and stamp[1] is not None
                    self._release = None
                    if event.type == self._x.KeyPress and event.detail == self._keycode and not repeated:
                        count += 1
            except Exception as error:
                raise BackendFailedError(f"读取 X11 热键失败: {error}") from error
            return count

    def close(self) -> None:
        with NATIVE_LOCK:
            if self._closed:
                return
            self._closed = True
            _close_display(self._display, self._ungrab)

    def _ungrab(self):
        failure = None
        for mask in self._masks:
            try:
                self._root.ungrab_key(self._keycode, mask)
            except Exception as error:
                failure = failure or error
        if failure is not None:
            raise failure


class HotkeyBackend:
    def __init__(self, runner=None) -> None:
        self._runner = runner or default_runner

    def register(self, keys: list[str]):
        parts = parse_hotkey(keys)
        if len(parts) < 2:
            raise BackendInvalidError("全局热键必须包含修饰键")
        modifiers = 0
        for key in parts[:-1]:
            modifiers |= _MODIFIERS[key]
        x, xk, display_type = _xlib()
        display = None
        listener = None
        try:
            with NATIVE_LOCK:
                display = display_type()
                root = display.screen().root
                key = parts[-1]
                keysym = xk.string_to_keysym(_SPECIAL_KEYS.get(key, key))
                if not keysym:
                    raise BackendInvalidError(f"X11 无法识别按键: {key}")
                keycode = display.keysym_to_keycode(keysym)
                if not keycode:
                    raise BackendInvalidError(f"当前键盘没有映射此按键: {key}")
                listener = _Listener(display, root, keycode, [], x)
                errors = []
                masks = sorted({modifiers | extra for extra in (
                    0, x.LockMask, x.Mod2Mask, x.LockMask | x.Mod2Mask,
                )})
                for mask in masks:
                    root.grab_key(keycode, mask, False, x.GrabModeAsync, x.GrabModeAsync,
                                  onerror=lambda error, request: errors.append(error))
                    listener._masks.append(mask)
                display.sync()
                if errors:
                    raise BackendFailedError(f"热键注册失败，组合键可能已被占用: {errors[0]}")
                return listener
        except Exception as error:
            if listener is not None:
                listener.close()
            elif display is not None:
                _close_display(display, lambda: None)
            if isinstance(error, BackendError):
                raise
            raise BackendFailedError(f"注册 X11 热键失败: {error}") from error

    def capture(self, timeout: float) -> list[str]:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError, OverflowError) as error:
            raise BackendInvalidError("热键录制超时必须是有限非负数") from error
        if not math.isfinite(timeout) or timeout < 0:
            raise BackendInvalidError("热键录制超时必须是有限非负数")
        if timeout == 0:
            return []
        x, xk, display_type = _xlib()
        display = None
        root = None
        try:
            with NATIVE_LOCK:
                display = display_type()
                root = display.screen().root
                status = root.grab_keyboard(False, x.GrabModeAsync, x.GrabModeAsync, x.CurrentTime)
                if status != x.GrabSuccess:
                    raise BackendFailedError(f"X11 键盘捕获失败: {status}")
                display.sync()
                names = {
                    xk.string_to_keysym(native): key for key, native in _SPECIAL_KEYS.items()
                }
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with NATIVE_LOCK:
                    if display.pending_events():
                        event = display.next_event()
                        if event.type != x.KeyPress:
                            continue
                        keysym = display.keycode_to_keysym(event.detail, 0)
                        if keysym == 0xFF1B:
                            return []
                        name = names.get(keysym) or xk.keysym_to_string(keysym) or ""
                        modifiers = [key for key, mask in _MODIFIERS.items() if event.state & mask]
                        if not modifiers or name.lower() == "capslock":
                            continue
                        try:
                            return parse_hotkey([*modifiers, name])
                        except BackendInvalidError:
                            continue
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            return []
        except BackendError:
            raise
        except Exception as error:
            raise BackendFailedError(f"录制 X11 热键失败: {error}") from error
        finally:
            if display is not None:
                _close_display(display, lambda: display.ungrab_keyboard(x.CurrentTime))
