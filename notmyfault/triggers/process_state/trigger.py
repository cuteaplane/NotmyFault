import time
import psutil


def run(trigger_info, config_list, emit_event):
    trigger_id = trigger_info.get("id")
    poll_interval = 2.0

    target_processes = set()
    original_names = {}
    for cfg in config_list:
        raw_name = cfg.get("process_name", "").strip()
        if not raw_name:
            continue

        if not raw_name.lower().endswith(".exe"):
            normalized_name = raw_name + ".exe"
        else:
            normalized_name = raw_name

        normalized_key = normalized_name.lower()
        target_processes.add(normalized_key)
        original_names[normalized_key] = raw_name

    if not target_processes:
        print(f"[Trigger:{trigger_id}] 没有需要监听的进程，触发器退出")
        return

    print(f"[Trigger:{trigger_id}] 开始监听进程: {sorted(original_names[process_name] for process_name in target_processes)}")

    last_states = {process_name: "stopped" for process_name in target_processes}

    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"]
            if name and name.lower() in target_processes:
                normalized_name = name.lower()
                last_states[normalized_name] = "running"
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
                emit_name = original_names.get(process_name, process_name)
                print(f"[Trigger:{trigger_id}] {emit_name} 状态变化: {current_state}")
                emit_event(trigger_id, {
                    "process_name": emit_name,
                    "state": current_state,
                })

        time.sleep(poll_interval)
