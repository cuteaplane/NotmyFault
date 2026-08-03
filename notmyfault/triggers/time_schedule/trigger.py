import time
from datetime import datetime


def _valid_time_format(value: str) -> bool:
    """校验 HH:MM 格式和真实时间范围，长度 5 且小时 00-23、分钟 00-59"""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "time_schedule")
    target_time = str(config.get("time", "") or "").strip().replace("：", ":")
    if not _valid_time_format(target_time):
        raise ValueError(
            f"未配置有效的触发时间: {target_time!r}（应为 HH:MM，24 小时制）"
        )

    print(f"[Trigger:{trigger_id}] 已设定触发时间: {target_time}")
    fired_on_date = None

    while not shutdown_event.is_set():
        try:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")

            if current_time == target_time and fired_on_date != today:
                print(f"[Trigger:{trigger_id}] 到达定时 {target_time}，触发！")
                emit_event({"triggered_time": target_time})
                fired_on_date = today
            elif current_time != target_time and fired_on_date == today:
                fired_on_date = None

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 检查出错: {e}")

        shutdown_event.wait(30)
