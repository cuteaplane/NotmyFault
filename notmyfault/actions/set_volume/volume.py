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
        from notmyfault.platform.backends import AudioBackend

        percent = round(scalar * 100)
        backend = AudioBackend()
        backend.set_volume(percent)
        backend.set_mute(action_lower == "mute")
        return

    from pycaw.pycaw import AudioUtilities

    device = AudioUtilities.GetSpeakers()
    if device is None:
        raise RuntimeError("未找到音频输出设备")
    endpoint = device.EndpointVolume

    if action_lower == "mute":
        # 静音通过 Mute 状态控制，音量标尺仍可保留
        endpoint.SetMute(True, None)
    else:
        endpoint.SetMasterVolumeLevelScalar(scalar, None)
        # 设置新音量前解除静音，Windows 可能保留旧 Mute 状态
        if endpoint.GetMute():
            endpoint.SetMute(False, None)

    # 读回验证：Windows 音频端点可能拒绝请求的级别
    final_scalar = endpoint.GetMasterVolumeLevelScalar()
    if action_lower == "mute":
        if not endpoint.GetMute():
            raise RuntimeError("Windows 拒绝设置静音状态")
    elif abs(final_scalar - scalar) > 0.01:
        raise RuntimeError(
            f"音量设置未生效（期望 {scalar:.2f}，实际 {final_scalar:.2f}）"
        )
