"""电源状态轮询与睡眠恢复监听"""

from notmyfault.plugin_api import platform
from notmyfault.triggers.base import PollingTrigger


class PowerStateTrigger(PollingTrigger):
    interval = 2.0
    native = False

    def validate(self):
        state = self.config.get("state", "ac")
        if state not in ("ac", "battery", "low_battery", "resume"):
            raise ValueError(
                f"无效的电源状态: {state!r}"
                "（可选: ac/battery/low_battery/resume）"
            )

    def setup(self):
        self.resume_monitor = None
        self.target_state = self.config.get("state", "ac")
        self.log(f"开始监控电源状态，目标: {self.target_state}")
        if self.target_state == "resume":
            self.resume_monitor = platform.power.resume_monitor()
            self.resume_monitor.start()

    def poll(self):
        resumed = self.resume_monitor is not None and self.resume_monitor.consume_resume()
        battery = platform.power.battery()
        if resumed and self.target_state == "resume":
            self.log("系统从睡眠中恢复")
            self.emit({"state": "resume", "battery_percent": battery["percent"]})
        self.emit({
            "state": "battery" if battery["on_battery"] else "ac",
            "battery_percent": battery["percent"],
        })

    def teardown(self):
        monitor = getattr(self, "resume_monitor", None)
        if monitor is not None:
            monitor.stop()
            self.resume_monitor = None


def run(meta, config, emit_event, shutdown_event):
    PowerStateTrigger(meta, config, emit_event, shutdown_event).run()
