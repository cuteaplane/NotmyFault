"""媒体控制动作：通过 WM_APPCOMMAND 发送播放、暂停、切歌和音量命令
先发送给前台窗口，再发送给 Shell_TrayWnd 系统托盘窗口；ctypes 调用声明类型并持有 NATIVE_LOCK
"""

import ctypes
from ctypes import wintypes

from notmyfault.native import NATIVE_LOCK

WM_APPCOMMAND = 0x0319
# SendMessageTimeout 标志：不等待挂起的窗口
SMTO_ABORTIFHUNG = 0x0002

_COMMANDS = {
    "play_pause": 47,
    "stop": 13,
    "next": 11,
    "previous": 12,
    "volume_up": 10,
    "volume_down": 9,
    "mute": 8,
}


def _send_appcommand(command: int) -> None:
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.DWORD),
    ]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

    # WM_APPCOMMAND 的 lParam：高 16 位是命令，低 16 位是标志
    lparam = (command << 16) | 0
    result = ctypes.c_ulong()

    hwnd = user32.GetForegroundWindow()
    if hwnd:
        user32.SendMessageTimeoutW(
            hwnd, WM_APPCOMMAND, 0, lparam,
            SMTO_ABORTIFHUNG, 1000, ctypes.byref(result),
        )

    # 前台窗口可能不处理媒体命令，例如资源管理器，再试一次系统托盘
    shell = user32.FindWindowW("Shell_TrayWnd", None)
    if shell:
        user32.SendMessageTimeoutW(
            shell, WM_APPCOMMAND, 0, lparam,
            SMTO_ABORTIFHUNG, 1000, ctypes.byref(result),
        )


def run(action_info, params):
    command = str(params.get("command", "play_pause") or "play_pause")
    if command not in _COMMANDS:
        raise ValueError(
            f"未知媒体命令: {command!r}（可选: {', '.join(_COMMANDS)}）"
        )
    print(f"[Action:media_control] 发送媒体命令: {command}")
    with NATIVE_LOCK:
        _send_appcommand(_COMMANDS[command])
    print(f"[Action:media_control] 已发送: {command}")
    return {"command": command}
