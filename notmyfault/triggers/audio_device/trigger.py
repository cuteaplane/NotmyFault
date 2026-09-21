"""音频设备变化触发器：默认播放或录音设备切换时发送事件
Windows 用 Core Audio API；Linux 用 wpctl 或 pactl
"""

import os

from notmyfault.plugin_api import PlatformServiceError, platform_services
from notmyfault.triggers.base import PollingTrigger



def _query_default_devices() -> dict[str, str]:
    """返回默认播放和录音设备 ID，查询失败返回空字典"""
    if os.name == "nt":
        return _query_default_devices_windows()
    return _query_default_devices_linux()


def _query_default_devices_windows() -> dict[str, str]:
    from comtypes import CoInitialize, CoUninitialize, COMError
    from pycaw.pycaw import AudioUtilities

    CoInitialize()
    try:
        enumerator = AudioUtilities.GetDeviceEnumerator()
        devices = {}
        for flow, name in ((0, "render"), (1, "capture")):
            try:
                devices[name] = enumerator.GetDefaultAudioEndpoint(flow, 0).GetId()
            except COMError as error:
                if error.hresult & 0xffffffff != 0x80070490:
                    raise RuntimeError("读取默认音频设备失败") from error
                devices[name] = ""
        return devices
    finally:
        CoUninitialize()


def _query_default_devices_linux() -> dict[str, str]:
    try:
        return platform_services().default_audio_devices()
    except PlatformServiceError:
        return {}


class AudioDeviceTrigger(PollingTrigger):
    """音频设备变化触发器，使用 event-v2 轮询"""

    interval: float = 5.0
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
            raise RuntimeError("无法查询默认音频设备，请检查音频服务")
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
                self.log(f"默认{flow}设备变化: {previous!r} -> {current!r}")
                self.emit({
                    "device_type": flow,
                    "device_id": current,
                    "previous_device_id": previous,
                    "changed": True,
                })
                self._last_ids[flow] = current


def run(meta, config, emit_event, shutdown_event):
    AudioDeviceTrigger(meta, config, emit_event, shutdown_event).run()
