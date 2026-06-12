from pycaw.pycaw import AudioUtilities

_VOLUME_LEVELS = {
    "max": 1.0,
    "half": 0.5,
    "min": 0.0,
    "mute": 0.0,
}

def set_volume(action):
    action_lower = str(action).lower().strip()

    scalar = _VOLUME_LEVELS.get(action_lower)

    if scalar is None:
        scalar = 0.5

    try:
        device = AudioUtilities.GetSpeakers()

        device.EndpointVolume.SetMasterVolumeLevelScalar(
            scalar,
            None
        )

    except Exception as e:
        print(f"[ERROR] set_volume failed: {e}")