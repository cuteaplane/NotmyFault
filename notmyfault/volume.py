# volume.py
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

def set_volume(action: str) -> None:
    try:
        devices = AudioUtilities.GetSpeakers()
        # 兼容不同 pycaw 版本
        if hasattr(devices, 'GetEndpointVolume'):
            volume = devices.GetEndpointVolume()
        elif hasattr(devices, 'Activate'):
            interface = devices.Activate(IAudioEndpointVolume._iid_, 0, None)
            volume = interface.QueryInterface(IAudioEndpointVolume)
        else:
            # 假设 devices 本身就是 IAudioEndpointVolume
            volume = devices
        
        action_map = {"max": 1.0, "half": 0.5, "min": 0.0}
        level = action_map.get(action)
        if level is not None:
            volume.SetMasterVolumeLevelScalar(level, None)
    except Exception as e:
        print(f"[ERROR] set_volume failed: {e}")  # 打印错误但不抛出