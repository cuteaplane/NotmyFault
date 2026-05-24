import time
import psutil


def run(trigger_info, params, call_notmyfault):
    process_name = params.get("process_name", "").strip()
    target_state = params.get("state", "running")
    poll_interval = float(params.get("poll_interval", 2))

    if not process_name:
        print(f"[Trigger:{trigger_info.get('id')}] 缺少 process_name，触发器退出")
        return

    if not process_name.lower().endswith(".exe"):
        process_name += ".exe"

    last_state = None
    print(f"[Trigger:{trigger_info.get('id')}] 开始监听 {process_name}，期望状态 {target_state}")

    while True:
        is_running = False
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() == process_name.lower():
                    is_running = True
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        current_state = "running" if is_running else "stopped"
        if current_state != last_state:
            last_state = current_state
            print(f"[Trigger:{trigger_info.get('id')}] 状态变化: {current_state}")
            if current_state == target_state:
                call_notmyfault({
                    "trigger_id": trigger_info.get("id"),
                    "triggered_params": params,
                })

        time.sleep(poll_interval)
