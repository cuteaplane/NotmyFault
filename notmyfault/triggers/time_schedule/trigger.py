import time
from datetime import datetime


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "time_schedule")
    target_time = config.get("time", "").strip().replace("：", ":")
    if not target_time or len(target_time) != 5 or target_time[2] != ":":
        print(f"[Trigger:{trigger_id}] 未配置触发时间，退出")
        return

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
