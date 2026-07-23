import ctypes
import subprocess

HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_ON = -1
MONITOR_OFF = 2


def _set_brightness(level: int):
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})"],
            capture_output=True, errors="replace", timeout=5,
        )
    except Exception:
        pass


def run(action_info, params):
    action = params.get("action", "off")
    user32 = ctypes.windll.user32

    print(f"[Action:display_control] 执行: {action}")

    try:
        if action == "off":
            user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF)
            print("[Action:display_control] 显示器已关闭")

        elif action == "on":
            user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_ON)
            print("[Action:display_control] 显示器已开启")

        elif action == "low_brightness":
            _set_brightness(10)
            print("[Action:display_control] 亮度已调低")

        elif action == "high_brightness":
            _set_brightness(90)
            print("[Action:display_control] 亮度已调高")

    except Exception as e:
        print(f"[Action:display_control] 操作失败: {e}")
