"""媒体控制动作
Windows 用 WM_APPCOMMAND；Linux 用 playerctl
"""

import os
import shutil
import subprocess


def _run_linux(command: str) -> None:
    playerctl_map = {
        "play_pause": "play-pause", "stop": "stop",
        "next": "next", "previous": "previous",
        "volume_up": "volume 0.05+", "volume_down": "volume 0.05-",
        "mute": "volume 0",
    }
    arg = playerctl_map.get(command)
    if arg is None:
        raise ValueError(f"未知媒体命令: {command!r}")
    tool = shutil.which("playerctl")
    if not tool:
        raise RuntimeError("缺少媒体控制后端，请安装 playerctl")
    parts = arg.split()
    result = subprocess.run(
        [tool] + parts,
        capture_output=True, text=True, errors="replace", timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "playerctl 执行失败")


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    from notmyfault.native import NATIVE_LOCK

    WM_APPCOMMAND = 0x0319
    SMTO_ABORTIFHUNG = 0x0002

    _COMMANDS = {
        "play_pause": 47, "stop": 13, "next": 11, "previous": 12,
        "volume_up": 10, "volume_down": 9, "mute": 8,
    }

    def _send_appcommand(command: int) -> None:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        lparam = (command << 16) | 0
        result = ctypes.c_size_t()
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            sent = user32.SendMessageTimeoutW(
                hwnd, WM_APPCOMMAND, 0, lparam,
                SMTO_ABORTIFHUNG, 1000, ctypes.byref(result),
            )
            if sent:
                return
        shell = user32.FindWindowW("Shell_TrayWnd", None)
        if shell:
            sent = user32.SendMessageTimeoutW(
                shell, WM_APPCOMMAND, 0, lparam,
                SMTO_ABORTIFHUNG, 1000, ctypes.byref(result),
            )
            if sent:
                return
        raise RuntimeError("没有窗口接受媒体命令")


def run(action_info, params):
    command = str(params.get("command", "play_pause") or "play_pause")
    print(f"[Action:media_control] 发送媒体命令: {command}")
    if os.name == "nt":
        if command not in _COMMANDS:
            raise ValueError(
                f"未知媒体命令: {command!r}（可选: {', '.join(_COMMANDS)}）"
            )
        with NATIVE_LOCK:
            _send_appcommand(_COMMANDS[command])
    else:
        _run_linux(command)
    print(f"[Action:media_control] 已发送: {command}")
    return {"command": command}
