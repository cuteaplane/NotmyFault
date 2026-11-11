from __future__ import annotations

import json
import queue
import sqlite3
import threading
from pathlib import Path

from notmyfault.core.value_codec import encode_value, decode_value


class StateStore:
    def __init__(self, path, *, capacity=1024):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS trigger_state (scope TEXT, key TEXT, value TEXT, PRIMARY KEY(scope,key))")
            self._values = {(scope, key): decode_value(json.loads(value)) for scope, key, value in connection.execute("SELECT scope,key,value FROM trigger_state")}
            connection.commit()
        finally:
            connection.close()
        self._lock = threading.RLock()
        self._writes = queue.Queue(capacity)
        self._closed = False
        self._thread = threading.Thread(target=self._write, name="StateWriter", daemon=True)
        self._thread.start()

    def get(self, scope, key, default=None):
        with self._lock:
            value = self._values.get((scope, key), default)
            return decode_value(encode_value(value))

    def set(self, scope, key, value, *, durable=False):
        encoded = json.dumps(encode_value(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        done = threading.Event() if durable else None
        outcome = {} if durable else None
        with self._lock:
            if self._closed:
                raise RuntimeError("状态存储已关闭")
            self._writes.put_nowait((scope, key, encoded, done, outcome))
            self._values[(scope, key)] = decode_value(json.loads(encoded))
        if done is not None:
            if not done.wait(5):
                raise TimeoutError("状态写入超时")
            if outcome.get("error") is not None:
                raise RuntimeError("状态写入失败") from outcome["error"]

    def _write(self):
        connection = None
        try:
            while True:
                item = self._writes.get()
                if item is None:
                    return
                try:
                    if connection is None:
                        connection = sqlite3.connect(self.path)
                    scope, key, value, _done, outcome = item
                    connection.execute("INSERT INTO trigger_state VALUES (?,?,?) ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value", (scope, key, value))
                    connection.commit()
                except Exception as error:
                    if item[4] is not None:
                        item[4]["error"] = error
                    connection = self._discard_connection(connection)
                finally:
                    if item[3] is not None:
                        item[3].set()
                    self._writes.task_done()
        except Exception as error:
            while True:
                try:
                    item = self._writes.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    if item[4] is not None:
                        item[4]["error"] = error
                    if item[3] is not None:
                        item[3].set()
                self._writes.task_done()
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _discard_connection(connection):
        # 写入失败后回滚并丢掉连接，下次写入重新连接，避免一次磁盘错误让存储永久不可用。
        if connection is None:
            return None
        try:
            connection.rollback()
        except Exception:
            pass
        try:
            connection.close()
        except Exception:
            pass
        return None

    def close(self, timeout=5):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._writes.put_nowait(None)
                except queue.Full:
                    self._closed = False
                    return False
        self._thread.join(timeout)
        return not self._thread.is_alive()


class TriggerState:
    def __init__(self, store, scope):
        self._store = store
        self._scope = scope

    def get(self, key, default=None):
        return self._store().get(self._scope, key, default)

    def set(self, key, value, *, durable=False):
        self._store().set(self._scope, key, value, durable=durable)
