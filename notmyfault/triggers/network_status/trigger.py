import socket
import math
import threading

from notmyfault.triggers.base import PollingTrigger


def _is_connected(host="8.8.8.8", port=53, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class NetworkStatusTrigger(PollingTrigger):
    interval = 5.0

    def validate(self):
        self.host = str(self.config.get("host", "8.8.8.8")).strip()
        try:
            self.port = int(self.config.get("port", 53))
            self.timeout = float(self.config.get("timeout", 2))
        except (TypeError, ValueError):
            raise ValueError("网络探测端口必须是整数，超时必须是数字") from None
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("网络探测主机或端口无效")
        if not math.isfinite(self.timeout) or not 0.1 <= self.timeout <= 10:
            raise ValueError("网络探测超时必须在 0.1 到 10 秒之间")
        self.target_state = self.config.get("state", "disconnected")
        if self.target_state not in ("connected", "disconnected"):
            raise ValueError(
                f"无效的网络状态: {self.target_state!r}（可选: connected/disconnected）"
            )

    def setup(self):
        self.log(f"开始监控网络状态，目标: {self.target_state}")

    def poll(self):
        if self._stop_event.is_set():
            return
        completed = threading.Event()
        result = []
        errors = []

        def sample():
            try:
                result.append(_is_connected(self.host, self.port, self.timeout))
            except Exception as error:
                errors.append(error)
            finally:
                completed.set()

        threading.Thread(target=sample, daemon=True).start()
        while not completed.wait(0.05):
            if self._stop_event.is_set():
                return
        if self._stop_event.is_set():
            return
        if errors:
            raise errors[0]
        current = result[0]
        self.emit({"state": "connected" if current else "disconnected"})


def run(meta, config, emit_event, shutdown_event):
    NetworkStatusTrigger(meta, config, emit_event, shutdown_event).run()
