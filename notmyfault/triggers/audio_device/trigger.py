from notmyfault.plugin_api import platform
from notmyfault.triggers.base import PollingTrigger



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

    def poll(self) -> None:
        devices = platform.audio.default_devices()
        if not devices:
            raise RuntimeError("无法查询默认音频设备，请检查音频服务")
        for flow in ("render", "capture"):
            if self.device_type != "any" and self.device_type != flow:
                continue
            current = devices.get(flow, "")
            self.emit({"device_type": flow, "device_id": current, "previous_device_id": current, "changed": True})


def run(meta, config, emit_event, shutdown_event):
    AudioDeviceTrigger(meta, config, emit_event, shutdown_event).run()
