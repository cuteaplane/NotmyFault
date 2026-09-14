from __future__ import annotations

from collections import deque
import os
import threading

from notmyfault.core.logging import get_latest_log, list_logs, parse_log_line


class LogFiles:
    def __init__(self, directory: str) -> None:
        self._directory = directory
        self._log_lock = threading.Lock()
        self._log_key = None
        self._log_offset = 0
        self._log_mtime = 0
        self._log_total = 0
        self._log_pending = b""
        self._log_tail: deque[str] = deque(maxlen=2000)

    def _path(self, name: str) -> str | None:
        if not name:
            return get_latest_log(self._directory)
        if not isinstance(name, str) or os.path.basename(name) != name or not (name.startswith("engine-") and name.endswith(".log")):
            raise ValueError("日志文件名无效")
        return os.path.join(self._directory, name)

    def list_files(self) -> list[dict]:
        return list_logs(self._directory)

    def entries(self, lines: int = 600, name: str = "") -> list[dict]:
        return [entry for line in self.tail(lines, name)["lines"] if (entry := parse_log_line(line)) is not None]

    def raw(self, lines: int = 300) -> str:
        return "\n".join(self.tail(lines)["lines"])

    def tail(self, lines: int, name: str = "") -> dict:
        safe_lines = min(max(int(lines), 1), 2000)
        log_path = self._path(name)
        if not log_path:
            return {"lines": [], "total": 0}
        try:
            with self._log_lock, open(log_path, "rb") as file:
                stat = os.fstat(file.fileno())
                key = (log_path, stat.st_dev, stat.st_ino)
                if key != self._log_key or stat.st_size < self._log_offset or (
                    stat.st_size == self._log_offset and stat.st_mtime_ns != self._log_mtime
                ):
                    self._log_key = key
                    self._log_offset = 0
                    self._log_total = 0
                    self._log_pending = b""
                    self._log_tail.clear()
                file.seek(self._log_offset)
                while chunk := file.read(65536):
                    parts = (self._log_pending + chunk).split(b"\n")
                    self._log_pending = parts.pop()
                    self._log_total += len(parts)
                    self._log_tail.extend(part.rstrip(b"\r").decode("utf-8", errors="replace") for part in parts)
                self._log_offset = file.tell()
                self._log_mtime = stat.st_mtime_ns
                tail = list(self._log_tail)
                if self._log_pending:
                    tail.append(self._log_pending.decode("utf-8", errors="replace"))
                total = self._log_total + bool(self._log_pending)
        except FileNotFoundError:
            return {"lines": [], "total": 0}
        return {
            "lines": tail[-safe_lines:],
            "total": total,
        }

