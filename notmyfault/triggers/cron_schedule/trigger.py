"""计划任务触发器：按天、按周或固定分钟间隔触发
daily 和 weekly 记录日期，interval 从触发器启动时刻开始计时
"""

import re
from datetime import datetime

from notmyfault.triggers.base import PollingTrigger

# 0 和 7 映射到 Python weekday 的周日值，其余数字按周一到周六映射
_WEEKDAYS = {
    "0": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}


class CronScheduleTrigger(PollingTrigger):
    """计划任务触发器，使用 event-v2 轮询"""

    interval: float = 15.0
    native: bool = False

    def validate(self) -> None:
        mode = self.config.get("mode", "daily")
        if mode not in ("daily", "weekly", "interval"):
            raise ValueError(
                f"无效的计划模式: {mode!r}（可选: daily/weekly/interval）"
            )
        self.mode = mode

        if mode in ("daily", "weekly"):
            raw_time = str(self.config.get("time", "") or "")
            if len(raw_time) == 8 and raw_time.endswith(":00"):
                raw_time = raw_time[:5]
            match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw_time)
            if not match:
                raise ValueError(f"无效的触发时间: {raw_time!r}（应为 HH:MM）")
            self.hour, self.minute = int(match.group(1)), int(match.group(2))
            self._fired_date: str | None = None

        if mode == "weekly":
            self.days = self._parse_days(self.config.get("days", "1,2,3,4,5"))
        elif mode == "interval":
            try:
                minutes = int(self.config.get("interval_minutes", 30))
            except (TypeError, ValueError):
                raise ValueError("interval_minutes 必须是整数") from None
            if minutes < 1:
                raise ValueError(f"interval_minutes 必须 >= 1，实际: {minutes}")
            self.interval_minutes = minutes
            self._last_fired: datetime | None = None

    @staticmethod
    def _parse_days(raw) -> list[int]:
        """把逗号分隔的星期编号转换为 Python weekday 数值"""
        days = []
        for part in str(raw or "").split(","):
            token = part.strip()
            if token not in _WEEKDAYS:
                raise ValueError(
                    f"无效的星期: {token!r}（可选 0-7，0/7=周日，逗号分隔）"
                )
            days.append(_WEEKDAYS[token])
        if not days:
            raise ValueError("weekly 模式必须指定至少一个星期")
        return days

    def poll(self) -> None:
        now = datetime.now()
        if self.mode == "daily":
            self._poll_daily(now)
        elif self.mode == "weekly":
            self._poll_weekly(now)
        else:
            self._poll_interval(now)

    def _poll_daily(self, now: datetime) -> None:
        date_key = now.strftime("%Y-%m-%d")
        if self._fired_date == date_key:
            return
        if (now.hour, now.minute) < (self.hour, self.minute):
            return
        # 到达或超过计划时间且当天尚未触发时记录日期并发送事件
        self._fired_date = date_key
        self._emit(now, "daily")

    def _poll_weekly(self, now: datetime) -> None:
        date_key = now.strftime("%Y-%m-%d")
        if self._fired_date == date_key:
            return
        if now.weekday() not in self.days:
            return
        if (now.hour, now.minute) < (self.hour, self.minute):
            return
        self._fired_date = date_key
        self._emit(now, "weekly")

    def _poll_interval(self, now: datetime) -> None:
        if self._last_fired is None:
            # 首次轮询记录起点，间隔从这一刻开始计时
            self._last_fired = now
            return
        elapsed = (now - self._last_fired).total_seconds()
        if elapsed < self.interval_minutes * 60:
            return
        self._last_fired = now
        self._emit(now, "interval")

    def _emit(self, now: datetime, mode: str) -> None:
        self.log(f"计划触发: mode={mode}, time={now.strftime('%H:%M')}")
        self.emit({
            "triggered_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
        })


def run(meta, config, emit_event, shutdown_event):
    CronScheduleTrigger(meta, config, emit_event, shutdown_event).run()
