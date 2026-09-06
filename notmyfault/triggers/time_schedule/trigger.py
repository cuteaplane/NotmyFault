from datetime import datetime

from notmyfault.triggers.base import PollingTrigger


def _valid_time_format(value: str) -> bool:
    """校验 HH:MM 格式和真实时间范围，长度 5 且小时 00-23、分钟 00-59"""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


class TimeScheduleTrigger(PollingTrigger):
    interval = 30.0

    def validate(self):
        target_time = str(self.config.get("time", "") or "").strip().replace("：", ":")
        if len(target_time) == 8 and target_time.endswith(":00"):
            target_time = target_time[:5]
        if not _valid_time_format(target_time):
            raise ValueError(
                f"未配置有效的触发时间: {target_time!r}（应为 HH:MM，24 小时制）"
            )
        self.target_time = target_time

    def setup(self):
        self._fired_on_date = None
        self.log(f"已设定触发时间: {self.target_time}")

    def poll(self):
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")
        if current_time == self.target_time and self._fired_on_date != today:
            self.log(f"到达定时 {self.target_time}，触发！")
            self.emit({"triggered_time": self.target_time})
            self._fired_on_date = today


def run(meta, config, emit_event, shutdown_event):
    TimeScheduleTrigger(meta, config, emit_event, shutdown_event).run()
