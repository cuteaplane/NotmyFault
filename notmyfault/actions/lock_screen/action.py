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
            print("[Action:lock_screen] 当前仅支持 Windows 平台")
    except Exception as e1:
        print(f"[Action:lock_screen] 直接调用失败: {e1}, 尝试 subprocess...")
        try:
            subprocess.run(
                ["rundll32.exe", "user32.dll,LockWorkStation"],
                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW
            )
            print("[Action:lock_screen] 屏幕已锁定 (via rundll32)")
        except Exception as e2:
            print(f"[Action:lock_screen] 锁定失败: {e2}")
