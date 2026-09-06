"""音频设备变化触发器：默认播放或录音设备切换时发送事件
Windows 用 PowerShell MediaDevice API；Linux 用 wpctl 或 pactl
"""

import os
import subprocess

from notmyfault.plugin_api import PlatformServiceError, platform_services
from notmyfault.triggers.base import PollingTrigger

_POWERSHELL_QUERY = r"""
[Windows.Media.Devices.MediaDevice, Windows.Media.Devices, ContentType = WindowsRuntime] | Out-Null
$r = [Windows.Media.Devices.MediaDevice]::GetDefaultAudioRenderId("Default")
$c = [Windows.Media.Devices.MediaDevice]::GetDefaultAudioCaptureId("Default")
Write-Output ("render=" + $r)
Write-Output ("capture=" + $c)
"""


def _query_default_devices() -> dict[str, str]:
    """返回默认播放和录音设备 ID，查询失败返回空字典"""
    if os.name == "nt":
        return _query_default_devices_windows()
    return _query_default_devices_linux()


def _query_default_devices_windows() -> dict[str, str]:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _POWERSHELL_QUERY,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    devices: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("render="):
            devices["render"] = line[len("render="):].strip() or ""
        elif line.startswith("capture="):
            devices["capture"] = line[len("capture="):].strip() or ""
    return devices


def _query_default_devices_linux() -> dict[str, str]:
    try:
        return platform_services().default_audio_devices()
    except PlatformServiceError:
        return {}


class AudioDeviceTrigger(PollingTrigger):
    """音频设备变化触发器，使用 event-v2 轮询"""

    interval: float = 5.0
    # PowerShell 子进程处理原生调用，native 设为 False
    native: bool = False

    def validate(self) -> None:
        device_type = self.config.get("device_type", "render")
        if device_type not in ("render", "capture", "any"):
            raise ValueError(
                f"无效的设备类型: {device_type!r}（可选: render/capture/any）"
            )
        self.device_type = device_type
        self._last_ids: dict[str, str] = {}

    def poll(self) -> None:
        devices = _query_default_devices()
        if not devices:
            # PowerShell 查询失败或超时就保持现状，下次再试
            return
        for flow in ("render", "capture"):
            if self.device_type != "any" and self.device_type != flow:
                continue
            current = devices.get(flow, "")
            previous = self._last_ids.get(flow)
            if previous is None:
                # 第一轮只记录当前状态，不触发
                self._last_ids[flow] = current
                continue
            if current != previous:
                self._last_ids[flow] = current
                self.log(f"默认{flow}设备变化: {previous!r} -> {current!r}")
                self.emit({
                    "device_type": flow,
                    "device_id": current,
                    "previous_device_id": previous,
                    "changed": True,
                })


def run(meta, config, emit_event, shutdown_event):
    AudioDeviceTrigger(meta, config, emit_event, shutdown_event).run()
