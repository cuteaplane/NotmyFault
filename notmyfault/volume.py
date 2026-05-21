from pycaw.pycaw import AudioUtilities

def set_volume(action: str) -> None:
    devices = AudioUtilities.GetSpeakers()
    # 新版 pycaw 可直接调用 GetEndpointVolume
    volume = devices.GetEndpointVolume()
    
    action_map = {
        "max": 1.0,
        "half": 0.5,
        "min": 0.0,
    }
    volume_level = action_map.get(action)
    if volume_level is not None:
        volume.SetMasterVolumeLevelScalar(volume_level, None)