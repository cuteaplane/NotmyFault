from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

# Supported volume action levels and their scalar values
_VOLUME_LEVELS = {
    "max": 1.0,
    "half": 0.5,
    "min": 0.0,
    "mute": 0.0,
}


def set_volume(action):
    action_lower = str(action).lower().strip() if action else ""
    scalar = _VOLUME_LEVELS.get(action_lower)

    if scalar is None:
        print(f"[WARN] set_volume: unknown action '{action}', defaulting to 'half'")
        scalar = 0.5

    try:
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(
            IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = interface.QueryInterface(IAudioEndpointVolume)
        volume.SetMasterVolumeLevelScalar(scalar, None)
    except Exception as e:
        print(f"[ERROR] set_volume failed: {e}")