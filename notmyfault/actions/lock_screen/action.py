import sys
import subprocess


def run(action_info, params):
    print("[Action:lock_screen] 正在锁定屏幕...")
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.LockWorkStation()
            print("[Action:lock_screen] 屏幕已锁定")
        else:
            result = subprocess.run(
                ["loginctl", "lock-session"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "loginctl lock-session 失败")
            print("[Action:lock_screen] 屏幕已锁定")
    except Exception as e1:
        print(f"[Action:lock_screen] 直接调用失败: {e1}, 尝试 subprocess...")
        if sys.platform != "win32":
            print(f"[Action:lock_screen] 锁定失败: {e1}")
            return
        try:
            subprocess.run(
                ["rundll32.exe", "user32.dll,LockWorkStation"],
                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW
            )
            print("[Action:lock_screen] 屏幕已锁定 (via rundll32)")
        except Exception as e2:
            print(f"[Action:lock_screen] 锁定失败: {e2}")
