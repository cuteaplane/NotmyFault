from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

def set_volume(action: str) -> None:
    try:
        devices = AudioUtilities.GetSpeakers()
        print(f"[DEBUG] Found audio device: {devices.GetId()}")
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))

        # 定义音量映射 (基于文章表格 [-65.25, 0.0] 的区间)
        volume_map = {
            "max": 0.0,      # 100% 音量
            "half": -10.3,   # 约50% 音量[reference:3]
            "min": -65.25,   # 静音
        }
        volume_level = volume_map.get(action, None)
        if volume_level is not None:
            # 使用 SetMasterVolumeLevel 设置音量
            volume.SetMasterVolumeLevel(volume_level, None)
    except Exception as e:
        print(f"[ERROR] set_volume failed: {e}")