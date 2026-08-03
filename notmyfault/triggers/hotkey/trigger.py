import ctypes
import time
from ctypes import wintypes

from notmyfault.triggers.base import PollingTrigger

user32 = ctypes.windll.user32
# ctypes 在多线程下共享 _objects 引用表，函数声明需完整
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
user32.DispatchMessageW.restype = ctypes.c_long

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
    'F13': 0x7C, 'F14': 0x7D, 'F15': 0x7E, 'F16': 0x7F,
    'F17': 0x80, 'F18': 0x81, 'F19': 0x82, 'F20': 0x83,
    'F21': 0x84, 'F22': 0x85, 'F23': 0x86, 'F24': 0x87,
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
    'ADD': 0x6B, 'SUBTRACT': 0x6D, 'MULTIPLY': 0x6A, 'DIVIDE': 0x6F,
    'OEM_1': 0xBA, 'OEM_PLUS': 0xBB, 'OEM_COMMA': 0xBC,
    'OEM_MINUS': 0xBD, 'OEM_PERIOD': 0xBE, 'OEM_2': 0xBF,
    'OEM_3': 0xC0, 'OEM_4': 0xDB, 'OEM_5': 0xDC,
    'OEM_6': 0xDD, 'OEM_7': 0xDE,
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
    """全局热键监听，注册后轮询线程消息队列"""

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
        if not user32.RegisterHotKey(None, self._hkid, self._mod, self._vk):
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
            user32.UnregisterHotKey(None, self._hkid)
        except Exception:
            pass


def run(meta, config, emit_event, shutdown_event):
    HotkeyTrigger(meta, config, emit_event, shutdown_event).run()
