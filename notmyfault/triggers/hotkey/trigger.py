"""全局热键触发器
Windows 用 RegisterHotKey；Linux 用 Xlib XGrabKey（仅 X11）
"""

import os

from notmyfault.plugin_api import native_lock, platform_backend_api

if os.name == "nt":
    import ctypes
    import time
    from ctypes import wintypes

    from notmyfault.triggers.base import PollingTrigger

    user32 = ctypes.windll.user32
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312

    _VK_MAP = {
        'F1': 0x70, 'F2': 0x71, 'F3': 0x72, 'F4': 0x73,
        'F5': 0x74, 'F6': 0x75, 'F7': 0x76, 'F8': 0x77,
        'F9': 0x78, 'F10': 0x79, 'F11': 0x7A, 'F12': 0x7B,
        'A': 0x41, 'B': 0x42, 'C': 0x43, 'D': 0x44,
        'E': 0x45, 'F': 0x46, 'G': 0x47, 'H': 0x48,
        'I': 0x49, 'J': 0x4A, 'K': 0x4B, 'L': 0x4C,
        'M': 0x4D, 'N': 0x4E, 'O': 0x4F, 'P': 0x50,
        'Q': 0x51, 'R': 0x52, 'S': 0x53, 'T': 0x54,
        'U': 0x55, 'V': 0x56, 'W': 0x57, 'X': 0x58,
        'Y': 0x59, 'Z': 0x5A,
        '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33,
        '4': 0x34, '5': 0x35, '6': 0x36, '7': 0x37,
        '8': 0x38, '9': 0x39,
        'RETURN': 0x0D, 'ENTER': 0x0D, 'ESCAPE': 0x1B, 'ESC': 0x1B,
        'TAB': 0x09, 'SPACE': 0x20, 'BACKSPACE': 0x08,
        'DELETE': 0x2E, 'DEL': 0x2E, 'INSERT': 0x2D, 'INS': 0x2D,
        'HOME': 0x24, 'END': 0x23, 'PAGEUP': 0x21, 'PAGEDOWN': 0x22,
        'UP': 0x26, 'DOWN': 0x28, 'LEFT': 0x25, 'RIGHT': 0x27,
        'CAPSLOCK': 0x14, 'NUMLOCK': 0x90,
        'F13': 0x7C, 'F14': 0x7D, 'F15': 0x7E, 'F16': 0x7F,
        'F17': 0x80, 'F18': 0x81, 'F19': 0x82, 'F20': 0x83,
        'F21': 0x84, 'F22': 0x85, 'F23': 0x86, 'F24': 0x87,
    }

    _MOD_MAP = {
        'ALT': MOD_ALT, 'CTRL': MOD_CONTROL, 'CONTROL': MOD_CONTROL,
        'SHIFT': MOD_SHIFT, 'WIN': MOD_WIN, 'SUPER': MOD_WIN,
    }

    def _parse_hotkey(hotkey_str: str):
        parts = [p.strip().upper() for p in hotkey_str.replace('+', ' ').split()]
        mod = 0
        key = 0
        for p in parts:
            if p in _MOD_MAP:
                mod |= _MOD_MAP[p]
            elif p in _VK_MAP:
                key = _VK_MAP[p]
        if key == 0 and parts:
            key = ord(parts[-1]) if len(parts[-1]) == 1 else 0
        return mod, key

    class HotkeyTrigger(PollingTrigger):
        interval = 0.05
        native = True

        def validate(self):
            raw = str(self.config.get("hotkey", "")).strip()
            if not raw:
                raise ValueError("未配置热键（hotkey 参数为空）")
            mod, vk = _parse_hotkey(raw)
            if vk == 0:
                raise ValueError(f"无法解析热键: {raw}")
            self._raw = raw
            self._mod = mod
            self._vk = vk

        def setup(self):
            self._hkid = 1
            with native_lock():
                if not user32.RegisterHotKey(None, self._hkid, self._mod | MOD_NOREPEAT, self._vk):
                    raise RuntimeError(f"热键注册失败（可能与其他程序冲突）: {self._raw}")
            self.log(f"已注册热键: {self._raw}")

        def poll(self):
            msg = wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
                if msg.message == WM_HOTKEY and msg.wParam == self._hkid:
                    self.log(f"热键触发: {self._raw}")
                    self.emit({"hotkey": self._raw})

        def teardown(self):
            try:
                with native_lock():
                    user32.UnregisterHotKey(None, self._hkid)
            except Exception:
                pass

else:
    from notmyfault.triggers.base import PollingTrigger

    # X11 修饰键掩码
    _X11_MOD_MAP = {
        'ctrl': 4, 'control': 4, 'shift': 1, 'alt': 8, 'super': 64, 'win': 64,
    }
    _X11_KEY_NAMES = {
        'f1': 'F1', 'f2': 'F2', 'f3': 'F3', 'f4': 'F4',
        'f5': 'F5', 'f6': 'F6', 'f7': 'F7', 'f8': 'F8',
        'f9': 'F9', 'f10': 'F10', 'f11': 'F11', 'f12': 'F12',
        'enter': 'Return', 'return': 'Return', 'tab': 'Tab',
        'space': 'space', 'backspace': 'BackSpace', 'delete': 'Delete',
        'escape': 'Escape', 'esc': 'Escape',
        'home': 'Home', 'end': 'End', 'pgup': 'Page_Up', 'pgdn': 'Page_Down',
        'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
        'capslock': 'Caps_Lock',
    }

    def _parse_x11_hotkey(raw: str):
        parts = [p.strip().lower() for p in raw.replace('+', ' ').split() if p.strip()]
        modifiers = 0
        main_key = None
        for part in parts:
            if part in _X11_MOD_MAP:
                modifiers |= _X11_MOD_MAP[part]
            else:
                main_key = _X11_KEY_NAMES.get(part, part)
        return modifiers, main_key

    class HotkeyTrigger(PollingTrigger):
        interval = 0.05
        native = False

        def validate(self):
            raw = str(self.config.get("hotkey", "")).strip()
            if not raw:
                raise ValueError("未配置热键（hotkey 参数为空）")
            self._raw = raw

        def setup(self):
            try:
                from Xlib import X, XK
                from Xlib.display import Display
            except ImportError:
                raise platform_backend_api().BackendMissingError(
                    "依赖缺失：全局热键需要 python-xlib（pip install python-xlib）"
                )
            modifiers, main_key_name = _parse_x11_hotkey(self._raw)
            if not main_key_name:
                raise ValueError(f"无法解析热键: {self._raw}")
            self._disp = Display()
            root = self._disp.screen().root
            keysym = XK.string_to_keysym(main_key_name)
            if keysym == 0:
                raise RuntimeError(f"X11 无法识别按键: {main_key_name}")
            keycode = self._disp.keysym_to_keycode(keysym)
            if keycode == 0:
                raise RuntimeError(f"当前键盘没有映射 {main_key_name} 的按键")
            # GrabModeAsync=1, AnyModifier=0 但我们自己指定修饰键
            root.grab_key(keycode, modifiers, False, X.GrabModeAsync, X.GrabModeAsync)
            # XGrabKey 不吃 NumLock/CapsLock，注册带锁的变体
            for extra in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
                if extra and (modifiers & extra) == 0:
                    root.grab_key(keycode, modifiers | extra, False,
                                  X.GrabModeAsync, X.GrabModeAsync)
            self._disp.sync()
            self._keycode = keycode
            self._triggered = False
            self.log(f"已注册热键: {self._raw}")

        def poll(self):
            from Xlib import X
            while True:
                event = self._disp.pending_events()
                if event == 0:
                    break
                ev = self._disp.next_event()
                if ev.type == X.KeyPress and ev.detail == self._keycode:
                    self._triggered = True
            if self._triggered:
                self._triggered = False
                self.log(f"热键触发: {self._raw}")
                self.emit({"hotkey": self._raw})

        def teardown(self):
            if hasattr(self, '_disp') and hasattr(self, '_keycode'):
                try:
                    root = self._disp.screen().root
                    root.ungrab_key(self._keycode, 0, False)
                    self._disp.sync()
                except Exception:
                    pass
            if hasattr(self, '_disp'):
                try:
                    self._disp.close()
                except Exception:
                    pass


def run(meta, config, emit_event, shutdown_event):
    HotkeyTrigger(meta, config, emit_event, shutdown_event).run()
