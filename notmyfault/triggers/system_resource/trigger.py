import math
import time
import psutil

from notmyfault.triggers.base import PollingTrigger


def _get_usage(resource: str) -> float:
    if resource == "cpu":
        return psutil.cpu_percent(interval=0)
    elif resource == "memory":
        return psutil.virtual_memory().percent
    elif resource == "disk":
        return psutil.disk_usage("/").percent
    elif resource == "network":
        net = psutil.net_io_counters()
        return (net.bytes_sent + net.bytes_recv) / (1024 * 1024)
    return 0.0


class SystemResourceTrigger(PollingTrigger):
    interval = 5.0

    def validate(self):
        self.resource = self.config.get("resource", "cpu")
        self.direction = self.config.get("direction", "above")
        if self.resource not in ("cpu", "memory", "disk", "network"):
            raise ValueError(
                f"无效的资源类型: {self.resource!r}（可选: cpu/memory/disk/network）"
            )
        if self.direction not in ("above", "below"):
            raise ValueError(
                f"无效的阈值方向: {self.direction!r}（可选: above/below）"
            )
        try:
            self.threshold = float(self.config.get("threshold", 90))
        except (TypeError, ValueError):
            raise ValueError(
                f"threshold 必须是数字，实际: {self.config.get('threshold')!r}"
            ) from None
        if not math.isfinite(self.threshold):
            raise ValueError("threshold 必须是有限数字")

    def setup(self):
        self._last_triggered = False
        self._net_prev = None
        self._net_rate = 0.0
        self._first_sample = True
        self.log(f"开始监控系统资源: {self.resource} {self.direction}")

    def poll(self):
        try:
            if self.resource == "network":
                net = psutil.net_io_counters()
                total = net.bytes_sent + net.bytes_recv
                now = time.time()
                if self._net_prev is not None:
                    elapsed = now - self._net_prev[0]
                    if elapsed > 0:
                        self._net_rate = (total - self._net_prev[1]) / elapsed / (1024 * 1024)
                self._net_prev = (now, total)
                value = self._net_rate
            else:
                value = _get_usage(self.resource)

            if self._first_sample:
                # cpu_percent 首次调用返回 0，network 首轮还没有速率值，首轮只记录状态
                self._first_sample = False
                return

            triggered = (self.direction == "above" and value >= self.threshold) or (
                self.direction == "below" and value <= self.threshold
            )
            if triggered and not self._last_triggered:
                unit = "MB/s" if self.resource == "network" else "%"
                self.log(
                    f"{self.resource} {self.direction} "
                    f"{self.threshold}{unit} (当前: {value:.1f})"
                )
                self.emit({
                    "resource": self.resource,
                    "value": round(value, 1),
                    "threshold": self.threshold,
                    "direction": self.direction,
                })
            self._last_triggered = triggered
        except Exception:
            self._last_triggered = False
            raise


def run(meta, config, emit_event, shutdown_event):
    SystemResourceTrigger(meta, config, emit_event, shutdown_event).run()
