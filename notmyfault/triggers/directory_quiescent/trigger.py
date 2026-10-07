"""目录在设定时间内没有匹配文件增删改时触发"""
import fnmatch
import math
import os
import re

from notmyfault.triggers.base import PollingTrigger


def _file_matches(pattern: str, name: str) -> bool:
    if not pattern or pattern == "*":
        return True
    if os.name == "nt":
        # Windows 文件系统大小写不敏感，fnmatch 默认大小写敏感会漏匹配
        regex = fnmatch.translate(pattern)
        return re.match(regex, name, re.IGNORECASE) is not None
    return fnmatch.fnmatch(name, pattern)


def _dir_snapshot(folder: str, pattern: str):
    snap = {}
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"监控目录不存在或不可读: {folder}")

    def raise_walk_error(error):
        raise error

    for root, _dirs, files in os.walk(folder, onerror=raise_walk_error):
        for name in files:
            if not _file_matches(pattern, name):
                continue
            path = os.path.join(root, name)
            stat = os.stat(path)
            snap[path] = (stat.st_size, stat.st_mtime_ns)
    return snap


class DirectoryQuiescentTrigger(PollingTrigger):
    interval = 2.0

    def validate(self):
        self.folder = str(self.config.get("folder_path", ""))
        if not self.folder or "\x00" in self.folder:
            raise ValueError("监控目录不能为空，且不能包含空字符")
        self.folder = os.path.abspath(os.path.expanduser(self.folder))
        if not os.path.isdir(self.folder):
            raise FileNotFoundError(f"监控目录不存在或不可读: {self.folder}")
        self.pattern = self.config.get("file_pattern", "*")
        if not isinstance(self.pattern, str):
            raise ValueError("文件名过滤必须是字符串")
        if not self.pattern.strip():
            self.pattern = "*"
        self.target_state = self.config.get("state", "quiescent")
        if self.target_state not in ("quiescent", "active", "changed"):
            raise ValueError("触发时机必须为 quiescent、active 或 changed")
        try:
            self.quiet_seconds = float(self.config.get("quiet_seconds", 30))
            self.interval = float(self.config.get("interval", 2))
        except (TypeError, ValueError):
            raise ValueError("静默时长和检查间隔必须是数字") from None
        if not math.isfinite(self.quiet_seconds) or not 0 <= self.quiet_seconds <= 86400:
            raise ValueError("静默时长必须是 0 到 86400 秒之间的数字")
        if not math.isfinite(self.interval) or not 0.2 <= self.interval <= 3600:
            raise ValueError("检查间隔必须是 0.2 到 3600 秒之间的数字")

    def setup(self):
        self._snapshot = _dir_snapshot(self.folder, self.pattern)
        self._last_change = self.clock.monotonic()
        self._read_error = False
        self.log(
            f"开始监视目录静默: {self.folder} "
            f"(静默={self.quiet_seconds}s, 过滤={self.pattern})"
        )

    def _emit_state(self, state: str, quiet_for: float, file_count: int):
        if self._stop_event.is_set():
            return
        self.emit({
            "state": state,
            "folder": self.folder,
            "quiet_for_seconds": quiet_for,
            "file_count": file_count,
        })

    def poll(self):
        try:
            new_snapshot = _dir_snapshot(self.folder, self.pattern)
        except OSError as error:
            if not self._read_error:
                self.log(f"无法读取目录状态，本轮跳过：{type(error).__name__}")
            self._read_error = True
            return
        self._read_error = False
        now = self.clock.monotonic()
        if new_snapshot != self._snapshot:
            self._snapshot = new_snapshot
            self._last_change = now
        quiet_for = now - self._last_change
        state = "quiescent" if quiet_for >= self.quiet_seconds else "active"
        self._emit_state(state, quiet_for, len(self._snapshot))


create_sampler = DirectoryQuiescentTrigger.from_context


def run(meta, config, emit_event, shutdown_event):
    DirectoryQuiescentTrigger(meta, config, emit_event, shutdown_event).run()
