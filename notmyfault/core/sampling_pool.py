from __future__ import annotations

import queue
import threading
from concurrent.futures import Future


class SamplingPool:
    def __init__(self, wake, *, workers=4, capacity=200):
        self._wake = wake
        self._capacity = capacity
        self._pending = 0
        self._lock = threading.Lock()
        self._jobs = queue.SimpleQueue()
        self._closed = False
        self._threads = [threading.Thread(target=self._run, name=f"TriggerSampler-{index}", daemon=True)
                         for index in range(workers)]
        self._started = False

    def _start(self):
        if not self._started:
            for thread in self._threads:
                if thread.ident is None:
                    thread.start()
            self._started = True

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("采样池已关闭")
            self._start()

    def submit(self, function, *args):
        with self._lock:
            if self._closed or self._pending >= self._capacity:
                return None
            self._start()
            future = Future()
            self._pending += 1
            self._jobs.put((future, function, args))
            return future

    def _run(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            future, function, args = job
            try:
                if future.set_running_or_notify_cancel():
                    future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)
            finally:
                with self._lock:
                    was_full = self._pending == self._capacity
                    self._pending -= 1
                if was_full:
                    self._wake()

    def close(self, timeout):
        with self._lock:
            self._closed = True
            threads = [thread for thread in self._threads if thread.ident is not None]
            if not threads:
                return True
            for thread in threads:
                self._jobs.put(None)
        import time

        deadline = time.monotonic() + timeout
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in threads)
