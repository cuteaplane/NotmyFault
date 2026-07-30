import time
from datetime import datetime


def run(meta, config_list, emit_event, shutdown_event):
    trigger_id = meta.get("id", "time_schedule")

    target_times = set()
    for cfg in config_list:
        t = cfg.get("time", "").strip().replace("：", ":")
        if t and len(t) == 5 and t[2] == ":":
            target_times.add(t)

    if not target_times:
        print(f"[Trigger:{trigger_id}] 未配置触发时间，退出")
        return

    print(f"[Trigger:{trigger_id}] 已设定触发时间: {sorted(target_times)}")

    # 记录每个时间点今天是否已触发过（key: "HH:MM" → date string）
    fired_on_date = {t: None for t in target_times}

    while not shutdown_event.is_set():
        try:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")

            for target in target_times:
                if current_time == target and fired_on_date[target] != today:
                    print(f"[Trigger:{trigger_id}] 到达定时 {target}，触发！")
                    emit_event(trigger_id, {"triggered_time": target})
                    fired_on_date[target] = today
                elif current_time != target:
                    # 跨过目标时间后重置标记（比如过了 22:01 就重置，为明天做准备）
                    if fired_on_date[target] == today:
                        fired_on_date[target] = None

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 检查出错: {e}")

        shutdown_event.wait(30)
