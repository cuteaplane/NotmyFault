import time
import os
import psutil


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "process_state")
    poll_interval = 2.0
    raw_name = config.get("process_name", "").strip()
    if not raw_name:
        print(f"[Trigger:{trigger_id}] 没有需要监听的进程，触发器退出")
        return
    normalized_name = raw_name + ".exe" if os.name == "nt" and not raw_name.lower().endswith(".exe") else raw_name
    target_process = normalized_name.lower()

    print(f"[Trigger:{trigger_id}] 开始监听进程: {raw_name}")
    target_state = config.get("state", "running")
    last_state = "stopped"

    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"]
            if name and name.lower() == target_process:
                last_state = "running"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    while not shutdown_event.is_set():
        currently_running = False
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info["name"]
                if name and name.lower() == target_process:
                    currently_running = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        current_state = "running" if currently_running else "stopped"
        if current_state != last_state:
            last_state = current_state
            # 按配置方向过滤，避免向规则层发送与目标无关的状态事件。
            if current_state != target_state:
                continue
            print(f"[Trigger:{trigger_id}] {raw_name} 状态变化: {current_state}")
            emit_event({"process_name": raw_name, "state": current_state})

        shutdown_event.wait(poll_interval)
