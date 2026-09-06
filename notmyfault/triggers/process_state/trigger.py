import os
import psutil

from notmyfault.triggers.base import PollingTrigger


class ProcessStateTrigger(PollingTrigger):
    interval = 2.0

    def validate(self):
        self.raw_name = self.config.get("process_name", "").strip()
        if not self.raw_name:
            raise ValueError("未配置监听的进程名（process_name 为空）")
        normalized_name = self.raw_name
        if os.name == "nt" and not normalized_name.lower().endswith(".exe"):
            normalized_name += ".exe"
        self.target_process = normalized_name.lower()
        self.target_state = self.config.get("state", "running")
        if self.target_state not in ("running", "stopped"):
            raise ValueError(
                f"无效的进程状态: {self.target_state!r}（可选: running/stopped）"
            )

    def _current_state(self):
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info["name"]
                if name and name.lower() == self.target_process:
                    return "running"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return "stopped"

    def setup(self):
        self._last_state = self._current_state()
        self.log(f"开始监听进程: {self.raw_name}")

    def poll(self):
        current_state = self._current_state()
        if current_state != self._last_state:
            self._last_state = current_state
            if current_state == self.target_state:
                self.log(f"{self.raw_name} 状态变化: {current_state}")
                self.emit({"process_name": self.raw_name, "state": current_state})


def run(meta, config, emit_event, shutdown_event):
    ProcessStateTrigger(meta, config, emit_event, shutdown_event).run()
