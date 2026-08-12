"""插件扩展命令使用的会话和受限上下文。"""

import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, Optional

from notmyfault.extensions.protocol import make_owned_value


SESSION_TTL_SECONDS = 30 * 60


class ExtensionSession:
    """保存一次插件页面交互的数据和可调用命令。"""

    def __init__(
        self,
        plugin_id: str,
        command_id: str,
        plugin_meta: Dict[str, Any],
        source_kind: str,
        source_id: str,
        allowed_commands: Iterable[str],
        data_type: Dict[str, Any],
        current_value: Any,
    ) -> None:
        self.plugin_id = plugin_id
        self.command_id = command_id
        self.plugin_meta = plugin_meta
        self.source_kind = source_kind
        self.source_id = source_id
        self.allowed_commands = frozenset(allowed_commands)
        self.data_type = data_type
        self.current_value = current_value
        self.session_id = uuid.uuid4().hex
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.data: Dict[str, Any] = {}
        self._status = ""
        self._cleanup_callbacks: list[Callable[[], Any]] = []
        self._closed = False
        self._lock = threading.RLock()

    def set_status(self, text: str) -> None:
        with self._lock:
            self._status = str(text or "")
            self.updated_at = time.time()

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def touch(self) -> None:
        with self._lock:
            self.updated_at = time.time()

    def add_cleanup(self, callback: Callable[[], Any]) -> None:
        with self._lock:
            if self._closed:
                callback()
                return
            self._cleanup_callbacks.append(callback)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            callbacks = list(reversed(self._cleanup_callbacks))
            self._cleanup_callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def invoke(
        self,
        handler: Callable[..., Any],
        context: Any,
        payload: Any,
    ) -> Any:
        with self._lock:
            self.updated_at = time.time()
            return handler(context, payload)


class ExtensionSessionManager:
    """保存活动扩展会话并清理超时项。"""

    def __init__(self, ttl_seconds: float = SESSION_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._sessions: Dict[str, ExtensionSession] = {}
        self._lock = threading.RLock()

    def create(self, **kwargs: Any) -> ExtensionSession:
        session = ExtensionSession(**kwargs)
        with self._lock:
            self._sessions[session.session_id] = session
        self._cleanup()
        return session

    def get(self, session_id: str) -> Optional[ExtensionSession]:
        self._cleanup()
        with self._lock:
            session = self._sessions.get(session_id)
        if session is not None:
            session.touch()
        return session

    def drop(self, session_id: str) -> Optional[ExtensionSession]:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()
        return session

    def drop_plugin(self, plugin_id: str) -> None:
        with self._lock:
            stale = [
                session_id
                for session_id, session in self._sessions.items()
                if session.plugin_id == plugin_id
            ]
            sessions = [self._sessions.pop(session_id) for session_id in stale]
        for session in sessions:
            session.close()

    def _cleanup(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if now - session.updated_at > self._ttl
            ]
            sessions = [self._sessions.pop(session_id) for session_id in expired]
        for session in sessions:
            session.close()


class ExtensionContext:
    """命令只能通过这组方法读取当前值、打开视图和提交数据。"""

    def __init__(self, session: ExtensionSession, registry: Any) -> None:
        self.session = session
        self._registry = registry

    @property
    def current_value(self) -> Any:
        return self.session.current_value

    def register_cleanup(self, callback: Callable[[], Any]) -> None:
        self.session.add_cleanup(callback)

    def open_view(self, view_id: str, state: Any = None) -> Dict[str, Any]:
        view = self._registry.view(self.session.plugin_id, view_id)
        if view is None:
            raise ValueError(f"插件没有声明视图: {view_id}")
        return {"ok": True, "view": view_id, "state": state}

    def commit(
        self,
        data: Any,
        summary: str,
        *,
        close: bool = True,
        response: Any = None,
    ) -> Dict[str, Any]:
        package_name = self.session.plugin_meta["package_name"]
        data_type = self.session.data_type
        value = make_owned_value(
            package_name,
            data_type["id"],
            data_type["version"],
            data,
            summary,
        )
        self.session.current_value = data
        return {
            "ok": True,
            "value": value,
            "data": response,
            "close": close,
        }

    def result(self, data: Any = None, *, close: bool = False) -> Dict[str, Any]:
        return {"ok": True, "data": data, "close": close}

    def error(self, message: str) -> Dict[str, Any]:
        return {"ok": False, "error": str(message)}
