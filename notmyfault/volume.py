from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

def set_volume(action):
    try:
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(
            IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = interface.QueryInterface(IAudioEndpointVolume)
        if action == "max":
            volume.SetMasterVolumeLevelScalar(1, None)
        elif action == "half":
            volume.SetMasterVolumeLevelScalar(0.5, None)
        elif action == "min":
            volume.SetMasterVolumeLevelScalar(0, None)
    except Exception as e:
        print(f"[ERROR] set_volume failed: {e}")