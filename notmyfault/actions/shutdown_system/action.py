import ctypes
import time
import threading
import os
import subprocess

EWX_LOGOFF = 0
EWX_SHUTDOWN = 0x00000001
EWX_REBOOT = 0x00000002
EWX_FORCE = 0x00000004
EWX_POWEROFF = 0x00000008

SE_SHUTDOWN_NAME = "SeShutdownPrivilege"


def _enable_shutdown_privilege():
    try:
        ADVAPI32 = ctypes.windll.advapi32
        KERNEL32 = ctypes.windll.kernel32
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

        token = ctypes.c_void_p()
        ADVAPI32.OpenProcessToken(KERNEL32.GetCurrentProcess(),
                                   TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                   ctypes.byref(token))
        luid = LUID()
        ADVAPI32.LookupPrivilegeValueW(None, SE_SHUTDOWN_NAME, ctypes.byref(luid))
        tp = TOKEN_PRIVILEGES(1, luid, SE_PRIVILEGE_ENABLED)
        ADVAPI32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
    except Exception:
        pass


def run(action_info, params):
    action = params.get("action", "shutdown")
    force = params.get("force", True)
    delay = params.get("delay_seconds", 0)

    flags_map = {
        "shutdown": EWX_SHUTDOWN | EWX_POWEROFF,
        "restart": EWX_REBOOT,
        "logoff": EWX_LOGOFF,
        "hibernate": 0,
        "sleep": 0,
    }

    flags = flags_map.get(action, EWX_SHUTDOWN | EWX_POWEROFF)
    if force and action in ("shutdown", "restart", "logoff"):
        flags |= EWX_FORCE

    _enable_shutdown_privilege()

    def _do_action():
        if delay > 0:
            time.sleep(delay)
        print(f"[Action:shutdown_system] 执行: {action}")
        if os.name != "nt":
            commands = {
                "shutdown": ["systemctl", "poweroff"],
                "restart": ["systemctl", "reboot"],
                "logoff": ["loginctl", "terminate-user", str(os.getuid())],
                "hibernate": ["systemctl", "hibernate"],
                "sleep": ["systemctl", "suspend"],
            }
            command = commands.get(action)
            if not command:
                print(f"[Action:shutdown_system] 不支持的操作: {action}")
                return
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
            )
            if result.returncode != 0:
                print(
                    "[Action:shutdown_system] 操作失败: "
                    + (result.stderr.strip() or f"退出码 {result.returncode}")
                )
            return
        if action == "hibernate":
            ctypes.windll.powrprof.SetSuspendState(True, True, False)
        elif action == "sleep":
            ctypes.windll.powrprof.SetSuspendState(False, True, False)
        else:
            ctypes.windll.user32.ExitWindowsEx(flags, 0)

    threading.Thread(target=_do_action, daemon=True).start()
