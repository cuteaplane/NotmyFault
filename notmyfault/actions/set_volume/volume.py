import os
import subprocess

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
        if os.name != "nt":
            percent = round(scalar * 100)
            result = subprocess.run(
                ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent}%"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "wpctl 设置音量失败")
            subprocess.run(
                ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if action_lower == "mute" else "0"],
                capture_output=True,
                timeout=5,
            )
            return

        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetSpeakers()

        device.EndpointVolume.SetMasterVolumeLevelScalar(
            scalar,
            None
        )

    except Exception as e:
        print(f"[ERROR] set_volume failed: {e}")
