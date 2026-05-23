import time
import psutil
from typing import Dict, Any

from .volume import set_volume
import Win_toaster.show_notification as show_notification


def is_process_running(process_name: str) -> bool:
    if not process_name.lower().endswith('.exe'):
        process_name += '.exe'
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] and process_name.lower() == proc.info["name"].lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def scan_processes(config: Dict[str, Any]) -> None:
    notified_processes = set()

    while True:
        for process in config.get("processes", []):
            process_name = process.get("process_name")
            if not process_name:
                continue

            running = is_process_running(process_name)
            if running and process_name not in notified_processes:
                set_volume(process.get("volume_action", "max"))
                notification = process.get("notification", {})
                show_notification(
                    notification.get("title", process_name),
                    notification.get("message", "")
                )
                notified_processes.add(process_name)

            if not running and process_name in notified_processes:
                notified_processes.remove(process_name)

        time.sleep(5)
