"""窗口置顶动作：把当前或指定窗口设为置顶或取消置顶
SetWindowPos 使用 HWND_TOPMOST 和 HWND_NOTOPMOST，ctypes 调用声明 argtypes 和 restype 并持有 NATIVE_LOCK
"""

import ctypes
from ctypes import wintypes

from notmyfault.native import NATIVE_LOCK

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# 模块加载时声明 argtypes 和 restype，调用时直接复用
user32 = ctypes.windll.user32
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL


def _find_windows_by_title(keyword: str):
    """通过标题模糊匹配查找所有可见窗口句柄"""
    found = []

    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if keyword.lower() in buf.value.lower():
            found.append(hwnd)
        return True

    # WNDPROC 回调必须保持引用存活，ctypes 才能继续调用它
    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return found


def _resolve_hwnd(params):
    """按 target 和 title 参数解析目标窗口句柄"""
    target = params.get("target", "active")
    if target == "title":
        title = str(params.get("title", "") or "").strip()
        if not title:
            raise ValueError("按标题匹配时必须填写窗口标题")
        hwnds = _find_windows_by_title(title)
        if not hwnds:
            raise RuntimeError(f"未找到标题包含 \"{title}\" 的可见窗口")
        if len(hwnds) > 1:
            print(f"[Action:window_pin] 匹配到 {len(hwnds)} 个窗口，操作第一个")
        return hwnds[0]

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError("没有活动窗口")
    return hwnd


def _is_pinned(hwnd) -> bool:
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    return bool(style & WS_EX_TOPMOST)


def run(action_info, params):
    action = str(params.get("action", "toggle") or "toggle")
    if action not in ("toggle", "pin", "unpin"):
        raise ValueError(f"未知操作: {action!r}（可选: toggle/pin/unpin）")

    with NATIVE_LOCK:
        hwnd = _resolve_hwnd(params)
        if action == "toggle":
            pin = not _is_pinned(hwnd)
        elif action == "pin":
            pin = True
        else:
            pin = False
        insert_after = HWND_TOPMOST if pin else HWND_NOTOPMOST
        ok = user32.SetWindowPos(
            hwnd,
            insert_after,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
        if not ok:
            raise RuntimeError("设置窗口置顶失败")

    state = "pinned" if pin else "unpinned"
    print(f"[Action:window_pin] {state} hwnd={hwnd}")
    return {"state": state, "hwnd": hwnd}
