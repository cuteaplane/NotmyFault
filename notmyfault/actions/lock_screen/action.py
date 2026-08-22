import ctypes
import subprocess
import sys


def run(action_info, params):
    print("[Action:lock_screen] 正在锁定屏幕...")
    if sys.platform == "win32":
        ok = ctypes.windll.user32.LockWorkStation()
        if not ok:
            raise RuntimeError("LockWorkStation 调用失败（可能被系统拒绝）")
        print("[Action:lock_screen] 屏幕已锁定")
        return

    from notmyfault.platform.linux_support import require_command

    loginctl = require_command("锁屏", "loginctl")
    result = subprocess.run(
        [loginctl, "lock-session"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "loginctl lock-session 失败")
    print("[Action:lock_screen] 屏幕已锁定")
