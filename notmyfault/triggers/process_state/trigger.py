import time
import psutil


def run(trigger_info, config_list, emit_event):
    trigger_id = trigger_info.get("id")
    poll_interval = 2.0

    target_processes = set()
    for cfg in config_list:
        process_name = cfg.get("process_name", "").strip()
        if not process_name:
            continue

        if not process_name.lower().endswith(".exe"):
            process_name += ".exe"

        target_processes.add(process_name.lower())

    if not target_processes:
        print(f"[Trigger:{trigger_id}] 没有需要监听的进程，触发器退出")
        return

    print(f"[Trigger:{trigger_id}] 开始监听进程: {sorted(target_processes)}")

    last_states = {process_name: "stopped" for process_name in target_processes}

    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"]
            if name and name.lower() in target_processes:
                last_states[name.lower()] = "running"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    while True:
        currently_running = set()
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info["name"]
                if name and name.lower() in target_processes:
                    currently_running.add(name.lower())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        for process_name in target_processes:
            current_state = "running" if process_name in currently_running else "stopped"
            if current_state != last_states[process_name]:
                last_states[process_name] = current_state
                print(f"[Trigger:{trigger_id}] {process_name} 状态变化: {current_state}")
                emit_event(trigger_id, {
                    "process_name": process_name,
                    "state": current_state,
                })

        time.sleep(poll_interval)
