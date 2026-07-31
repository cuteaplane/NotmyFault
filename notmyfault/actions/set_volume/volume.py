import os
import subprocess

_VOLUME_LEVELS = {
    "max": 1.0,
    "half": 0.5,
    "min": 0.0,
    "mute": 0.0,
}

_KNOWN_ACTIONS = ("max", "half", "min", "mute")


def set_volume(action):
    action_lower = str(action).lower().strip()

    if action_lower not in _KNOWN_ACTIONS:
        raise ValueError(
            f"未知音量操作: {action_lower}（可选: {', '.join(_KNOWN_ACTIONS)}）"
        )

    scalar = _VOLUME_LEVELS[action_lower]

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
        result = subprocess.run(
            ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if action_lower == "mute" else "0"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "wpctl 设置静音失败")
        return

    from pycaw.pycaw import AudioUtilities

    device = AudioUtilities.GetSpeakers()
    if device is None:
        raise RuntimeError("未找到音频输出设备")
    endpoint = device.EndpointVolume

    if action_lower == "mute":
        # 静音 = 设置 Mute 状态，而不是把音量调到 0（两者不等价）。
        endpoint.SetMute(True, None)
    else:
        endpoint.SetMasterVolumeLevelScalar(scalar, None)
        # 取消静音，避免“设为 50%”时仍处于静音状态。
        if endpoint.GetMute():
            endpoint.SetMute(False, None)

    # 读回验证：Windows 音频端点可能拒绝请求的级别。
    final_scalar = endpoint.GetMasterVolumeLevelScalar()
    if action_lower == "mute":
        if not endpoint.GetMute():
            raise RuntimeError("Windows 拒绝设置静音状态")
    elif abs(final_scalar - scalar) > 0.01:
        raise RuntimeError(
            f"音量设置未生效（期望 {scalar:.2f}，实际 {final_scalar:.2f}）"
        )
