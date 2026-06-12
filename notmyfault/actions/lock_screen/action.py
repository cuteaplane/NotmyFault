import ctypes
import sys


def run(action_info, params):
    print("[Action:lock_screen] 正在锁定屏幕...")
    try:
        if sys.platform == "win32":
            ctypes.windll.user32.LockWorkStation()
            print("[Action:lock_screen] 屏幕已锁定")
        else:
            print("[Action:lock_screen] 当前仅支持 Windows 平台")
    except Exception as e:
        print(f"[Action:lock_screen] 锁定失败: {e}")
