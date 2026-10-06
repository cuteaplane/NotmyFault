"""电量阈值触发器：电量跨过设定阈值时触发一次
使用 psutil.sensors_battery()，无电池设备跳过采样；触发后需离开阈值 5 个百分点才再次允许触发
"""

import psutil

from notmyfault.triggers.base import PollingTrigger


class BatteryLevelTrigger(PollingTrigger):
    """电量阈值触发器，使用 event-v2 轮询"""

    interval: float = 30.0
    native: bool = False

    def validate(self) -> None:
        direction = self.config.get("direction", "below")
        if direction not in ("below", "above"):
            raise ValueError(
                f"无效的触发方向: {direction!r}（可选: below/above）"
            )
        charge_state = self.config.get("charge_state", "any")
        if charge_state not in ("any", "charging", "discharging"):
            raise ValueError(
                f"无效的电源状态过滤: {charge_state!r}"
                "（可选: any/charging/discharging）"
            )
        try:
            threshold = float(self.config.get("threshold", 20))
        except (TypeError, ValueError):
            raise ValueError("阈值必须是数字") from None
        if not 0 <= threshold <= 100:
            raise ValueError(f"阈值必须在 0-100 之间，实际: {threshold}")
        self.direction = direction
        self.charge_state = charge_state
        self.threshold = threshold

    def poll(self) -> None:
        try:
            battery = psutil.sensors_battery()
        except (NotImplementedError, OSError) as error:
            raise RuntimeError("无法读取电池状态，系统可能未提供电池接口") from error
        if battery is None:
            return
        percent = round(battery.percent)
        state = "charging" if battery.power_plugged else "discharging"

        if self.charge_state == "charging" and not battery.power_plugged:
            return
        if self.charge_state == "discharging" and battery.power_plugged:
            return

        self.emit({"battery_percent": percent, "state": state, "threshold": self.threshold, "direction": self.direction,
                   "matched": percent <= self.threshold if self.direction == "below" else percent >= self.threshold})


def run(meta, config, emit_event, shutdown_event):
    BatteryLevelTrigger(meta, config, emit_event, shutdown_event).run()
