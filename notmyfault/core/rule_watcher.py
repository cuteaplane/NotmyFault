from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from notmyfault.core.clock import Clock, SystemClock


@dataclass(frozen=True, slots=True)
class FileStamp:
    mtime_ns: int
    size: int
    sha256: str


class RuleWatcher:
    def __init__(self, path, load, apply, *, clock: Clock | None = None,
                 debounce: float = 0.3, poll_interval: float = 0.25, on_error=None,
                 verify_interval: float = 1.0) -> None:
        self._path = path
        self._load = load
        self._apply = apply
        self._clock = clock or SystemClock()
        self._debounce = debounce
        self._poll_interval = poll_interval
        self._on_error = on_error or (lambda error, stage: None)
        self._lock = threading.Lock()
        self._stamp: FileStamp | None = None
        self._pending: FileStamp | None = None
        self._has_pending = False
        self._read_cache = None
        self._read_lock = threading.Lock()
        self._verify_interval = verify_interval
        self._verified_at = float("-inf")
        self._deadline = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_error = None
        self._initialized = False

    def read_stamp(self) -> FileStamp | None:
        path = Path(self._path())
        with self._read_lock:
            try:
                stat = path.stat()
                key = (str(path), stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size)
                now = self._clock.monotonic()
                if self._read_cache is not None and self._read_cache[0] == key and now - self._verified_at < self._verify_interval:
                    return self._read_cache[1]
                data = path.read_bytes()
                after = path.stat()
            except FileNotFoundError:
                self._read_cache = None
                return None
            if (stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size) != (after.st_dev, after.st_ino, after.st_ctime_ns, after.st_mtime_ns, after.st_size) or len(data) != after.st_size:
                raise OSError("规则文件仍在写入")
            stamp = FileStamp(after.st_mtime_ns, after.st_size, hashlib.sha256(data).hexdigest())
            self._read_cache = (key, stamp)
            self._verified_at = now
            return stamp

    def acknowledge(self) -> None:
        stamp = self.read_stamp()
        with self._lock:
            self._stamp = stamp
            self._initialized = True
            self._pending = None
            self._has_pending = False
            self._last_error = None

    def _report(self, error, stage):
        key = (type(error), str(error), stage)
        with self._lock:
            if self._last_error == key:
                return
            self._last_error = key
        self._on_error(error, stage)

    def check_once(self) -> bool:
        try:
            stamp = self.read_stamp()
        except OSError as error:
            self._report(error, "read")
            return False
        now = self._clock.monotonic()
        with self._lock:
            if stamp == self._stamp:
                self._pending = None
                self._has_pending = False
                return False
            if not self._has_pending or stamp != self._pending:
                self._pending = stamp
                self._has_pending = True
                self._deadline = now + self._debounce
                return False
            if now < self._deadline:
                return False
            previous = self._stamp
            self._stamp = stamp
            self._pending = None
            self._has_pending = False
        if stamp is not None and previous is not None and stamp.sha256 == previous.sha256:
            return False
        try:
            document = self._load()
        except Exception as error:
            self._report(error, "read")
            return False
        try:
            self._apply(document, "file")
        except Exception as error:
            self._report(error, "apply")
            return False
        with self._lock:
            self._last_error = None
        return True

    def start(self) -> None:
        if not self._initialized:
            self.acknowledge()
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="RulesWatcher", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._clock.wait(self._stop, self._poll_interval):
            self.check_once()

    def stop(self, timeout: float = 2.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()
