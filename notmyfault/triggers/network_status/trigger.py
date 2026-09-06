import socket

from notmyfault.triggers.base import PollingTrigger


def _is_connected(host="8.8.8.8", port=53, timeout=2):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((host, port))
        return True
    except (socket.error, OSError):
        return False
    finally:
        s.close()


class NetworkStatusTrigger(PollingTrigger):
    interval = 5.0

    def validate(self):
        self.target_state = self.config.get("state", "disconnected")
        if self.target_state not in ("connected", "disconnected"):
            raise ValueError(
                f"无效的网络状态: {self.target_state!r}（可选: connected/disconnected）"
            )

    def setup(self):
        self._last_connected = _is_connected()
        self.log(f"开始监控网络状态，目标: {self.target_state}")
        self.log(f"初始网络状态: {'已连接' if self._last_connected else '已断开'}")

    def poll(self):
        current = _is_connected()
        if current != self._last_connected:
            new_state = "connected" if current else "disconnected"
            if new_state == self.target_state:
                self.log(f"网络状态变化: {new_state}")
                self.emit({"state": new_state})
            self._last_connected = current


def run(meta, config, emit_event, shutdown_event):
    NetworkStatusTrigger(meta, config, emit_event, shutdown_event).run()
