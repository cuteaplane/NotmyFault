import math
import socket
import threading

from notmyfault.triggers.base import PollingTrigger


class TcpPortTrigger(PollingTrigger):
    def validate(self):
        host = self.config.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host.strip() or "\x00" in host:
            raise ValueError("主机名或 IP 地址不能为空，且不能包含空字符")
        self.host = host.strip()
        try:
            self.host.encode("idna")
        except UnicodeError:
            raise ValueError("主机名无法转换为有效的域名") from None
        try:
            port = float(self.config.get("port", 80))
            self.interval = float(self.config.get("interval", 5))
            self.timeout = float(self.config.get("timeout", 1))
        except (TypeError, ValueError):
            raise ValueError("端口、检查间隔和连接超时必须是数字") from None
        if not math.isfinite(port) or not port.is_integer() or not 1 <= port <= 65535:
            raise ValueError("端口必须是 1 到 65535 之间的整数")
        self.port = int(port)
        if not math.isfinite(self.interval) or not 0.2 <= self.interval <= 3600:
            raise ValueError("检查间隔必须在 0.2 到 3600 秒之间")
        if not math.isfinite(self.timeout) or not 0.1 <= self.timeout <= 10:
            raise ValueError("连接超时必须在 0.1 到 10 秒之间")
        self.target_state = self.config.get("state", "reachable")
        if self.target_state not in ("reachable", "unreachable", "changed"):
            raise ValueError("目标状态必须为 reachable、unreachable 或 changed")

    def setup(self):
        self._previous = None

    def poll(self):
        completed = threading.Event()
        result = []

        def sample():
            try:
                with socket.create_connection((self.host, self.port), timeout=self.timeout):
                    result.append(True)
            except OSError:
                result.append(False)
            finally:
                completed.set()

        # 系统的域名解析不受 socket 超时控制，采样线程可独立等待解析返回。
        threading.Thread(target=sample, daemon=True).start()
        while not completed.wait(0.05):
            if self._stop_event.is_set():
                return
        if self._stop_event.is_set() or not result:
            return
        reachable = result[0]
        previous = self._previous
        self._previous = reachable
        state = "reachable" if reachable else "unreachable"
        if (
            previous is not None
            and reachable != previous
            and self.target_state in (state, "changed")
            and not self._stop_event.is_set()
        ):
            self.emit({"host": self.host, "port": self.port, "state": state, "reachable": reachable})


def run(meta, config, emit_event, shutdown_event):
    TcpPortTrigger(meta, config, emit_event, shutdown_event).run()
