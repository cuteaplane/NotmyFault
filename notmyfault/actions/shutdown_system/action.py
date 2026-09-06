"""系统关机、重启、注销、休眠和睡眠动作
force 默认关闭，params.confirm 必须为 true，执行失败时抛异常
"""

import ctypes
import os
import subprocess
import time

from notmyfault.plugin_api import native_lock

EWX_LOGOFF = 0
EWX_SHUTDOWN = 0x00000001
EWX_REBOOT = 0x00000002
EWX_FORCE = 0x00000004
EWX_POWEROFF = 0x00000008

SE_SHUTDOWN_NAME = "SeShutdownPrivilege"

_ALLOWED_ACTIONS = ("shutdown", "restart", "logoff", "hibernate", "sleep")

_LINUX_COMMANDS = {
    "shutdown": ["systemctl", "poweroff"],
    "restart": ["systemctl", "reboot"],
    "logoff": ["loginctl", "terminate-user"],
    "hibernate": ["systemctl", "hibernate"],
    "sleep": ["systemctl", "suspend"],
}


def _enable_shutdown_privilege():
    try:
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x2

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [
                ("PrivilegeCount", ctypes.c_ulong),
                ("Luid", LUID),
                ("Attributes", ctypes.c_ulong),
            ]

        with native_lock():
            advapi32 = ctypes.windll.advapi32
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            advapi32.OpenProcessToken.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
            ]
            advapi32.OpenProcessToken.restype = ctypes.c_int
            advapi32.LookupPrivilegeValueW.argtypes = [
                ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(LUID),
            ]
            advapi32.LookupPrivilegeValueW.restype = ctypes.c_int
            advapi32.AdjustTokenPrivileges.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(TOKEN_PRIVILEGES),
                ctypes.c_uint32, ctypes.POINTER(TOKEN_PRIVILEGES),
                ctypes.POINTER(ctypes.c_uint32),
            ]
            advapi32.AdjustTokenPrivileges.restype = ctypes.c_int

            token = ctypes.c_void_p()
            if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(),
                TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                ctypes.byref(token),
            ):
                return
            try:
                luid = LUID()
                if not advapi32.LookupPrivilegeValueW(
                    None, SE_SHUTDOWN_NAME, ctypes.byref(luid)
                ):
                    return
                tp = TOKEN_PRIVILEGES(1, luid, SE_PRIVILEGE_ENABLED)
                advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
            finally:
                kernel32.CloseHandle(token)
    except Exception:
        pass


def _execute(params, cancellation=None):
    action = params.get("action", "shutdown")
    force = params.get("force", False)
    confirm = params.get("confirm", False)
    delay = params.get("delay_seconds", 0)

    try:
        delay = max(0.0, float(delay))
    except (TypeError, ValueError):
        raise ValueError(
            f"delay_seconds 必须是数字，实际: {delay!r}"
        ) from None

    if action not in _ALLOWED_ACTIONS:
        raise ValueError(
            f"不支持的操作: {action}（可选: {', '.join(_ALLOWED_ACTIONS)}）"
        )
    if confirm is not True:
        raise PermissionError(
            "shutdown_system 需要显式设置 confirm=true 才会执行，防止误触发关机"
        )
    if not isinstance(force, bool):
        raise ValueError(f"force 必须为布尔值，实际: {force!r}")

    if delay > 0:
        print(f"[Action:shutdown_system] 等待 {delay}s 后执行: {action}")
        if cancellation is not None:
            if cancellation.wait(delay):
                cancellation.raise_if_cancelled()
        else:
            time.sleep(delay)

    if cancellation is not None:
        cancellation.raise_if_cancelled()

    print(f"[Action:shutdown_system] 执行: {action} (force={force})")

    if os.name != "nt":
        command = _LINUX_COMMANDS[action]
        if action == "logoff":
            getuid = getattr(os, "getuid", None)
            if getuid is None:
                raise RuntimeError("当前系统无法获取用户 ID，不能注销")
            command = command + [str(getuid())]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"操作失败: {result.stderr.strip() or f'退出码 {result.returncode}'}"
            )
        return

    _enable_shutdown_privilege()
    with native_lock():
        if action in ("hibernate", "sleep"):
            powrprof = ctypes.windll.powrprof
            powrprof.SetSuspendState.argtypes = [ctypes.c_ubyte] * 3
            powrprof.SetSuspendState.restype = ctypes.c_ubyte
            ok = powrprof.SetSuspendState(action == "hibernate", True, False)
        else:
            flags = {
                "shutdown": EWX_SHUTDOWN | EWX_POWEROFF,
                "restart": EWX_REBOOT,
                "logoff": EWX_LOGOFF,
            }[action]
            if force:
                flags |= EWX_FORCE
            user32 = ctypes.windll.user32
            user32.ExitWindowsEx.argtypes = [ctypes.c_uint, ctypes.c_uint32]
            user32.ExitWindowsEx.restype = ctypes.c_int
            ok = user32.ExitWindowsEx(flags, 0)
    if not ok:
        raise RuntimeError(
            f"系统未能执行 {action}（可能被其他程序阻止或权限不足）"
        )


def run(action_info, params):
    return _execute(params)


def run_with_context(action_info, params, context):
    runtime = context.get("runtime", {})
    cancellation = runtime.get("cancellation") if isinstance(runtime, dict) else None
    if cancellation is None:
        raise RuntimeError("shutdown_system 缺少 runtime-v1 取消对象")
    return _execute(params, cancellation)
