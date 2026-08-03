import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_RESTORE = 9

WM_CLOSE = 0x0010

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _find_windows_by_title(keyword):
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

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return found


def _resolve_hwnd(params):
    """根据 target 和 title 参数解析目标窗口句柄"""
    target = params.get("target", "active")
    title = params.get("title", "").strip()

    if target == "title" and title:
        hwnds = _find_windows_by_title(title)
        if not hwnds:
            print(f"[Action:window_control] 未找到标题包含 \"{title}\" 的可见窗口")
            return 0
        if len(hwnds) > 1:
            print(f"[Action:window_control] 匹配到 {len(hwnds)} 个窗口，操作第一个")
        return hwnds[0]

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        print("[Action:window_control] 没有活动窗口")
    return hwnd


def run(action_info, params):
    action = params.get("action", "bring_to_front")
    hwnd = _resolve_hwnd(params)
    if not hwnd:
        return

    print(f"[Action:window_control] 操作: {action}, hwnd: {hwnd}")

    try:
        if action == "bring_to_front":
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)

        elif action == "minimize":
            user32.ShowWindow(hwnd, SW_SHOWMINIMIZED)

        elif action == "maximize":
            user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)

        elif action == "restore":
            user32.ShowWindow(hwnd, SW_RESTORE)

        elif action == "close":
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

        elif action == "hide":
            user32.ShowWindow(hwnd, SW_HIDE)

        elif action == "move":
            x = int(params.get("x", 0))
            y = int(params.get("y", 0))
            user32.SetWindowPos(hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER)

        elif action == "resize":
            width = int(params.get("width", 800))
            height = int(params.get("height", 600))
            user32.SetWindowPos(hwnd, 0, 0, 0, width, height, SWP_NOMOVE | SWP_NOZORDER)

        else:
            print(f"[Action:window_control] 未知操作: {action}")
            return

        print(f"[Action:window_control] 操作完成: {action}")

    except Exception as e:
        print(f"[Action:window_control] 操作失败: {e}")
