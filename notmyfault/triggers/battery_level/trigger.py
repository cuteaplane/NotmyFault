"""电量阈值触发器：电量跨过设定阈值时触发一次
使用 psutil.sensors_battery()，无电池设备跳过采样；触发后需离开阈值 5 个百分点才再次允许触发
"""

import psutil

from notmyfault.triggers.base import PollingTrigger


class BatteryLevelTrigger(PollingTrigger):
    """电量阈值触发器，使用 event-v2 轮询"""

    interval: float = 30.0
    native: bool = False

    # 再次允许触发的阈值距离为 5 个百分点
    HYSTERESIS = 5

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
        # 初始允许触发，引擎启动后第一次跨过阈值就发送事件
        self._armed = True

    def poll(self) -> None:
        battery = psutil.sensors_battery()
        if battery is None:
            # 无电池设备没有可观测电量，本轮返回并保持 _armed
            return
        percent = round(battery.percent)
        state = "charging" if battery.power_plugged else "discharging"

        # 电源状态不匹配时本轮返回并保持 _armed
        if self.charge_state == "charging" and not battery.power_plugged:
            return
        if self.charge_state == "discharging" and battery.power_plugged:
            return

        if self.direction == "below":
            if self._armed and percent <= self.threshold:
                self._emit(percent, state)
                self._armed = False
            elif not self._armed and percent > self.threshold + self.HYSTERESIS:
                # 电量超过阈值加迟滞后再次允许触发
                self._armed = True
        else:
            if self._armed and percent >= self.threshold:
                self._emit(percent, state)
                self._armed = False
            elif not self._armed and percent < self.threshold - self.HYSTERESIS:
                self._armed = True

    def _emit(self, percent: int, state: str) -> None:
        self.log(f"电量跨过阈值 {self.threshold}%（当前 {percent}%，{state}）")
        self.emit({
            "battery_percent": percent,
            "state": state,
            "threshold": self.threshold,
            "direction": self.direction,
        })


def run(meta, config, emit_event, shutdown_event):
    BatteryLevelTrigger(meta, config, emit_event, shutdown_event).run()
