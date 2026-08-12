"""组件会话与活动会话管理，插件组件通过 ComponentSession 在调用间保持状态"""

import threading
import time
import uuid
from typing import Any, Dict, List, Optional

SESSION_TTL_SECONDS = 30 * 60


class ComponentSession:
    """一次组件交互会话。组件往里面存中间数据，NotmyFault 在超时后清理。"""

    def __init__(
        self,
        plugin_id: str,
        component_id: str,
        meta: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.component_id = component_id
        self.meta = meta
        self.session_id = session_id or uuid.uuid4().hex
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.data: Dict[str, Any] = {}
        self._status = ""
        self._finalized = False
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

    def mark_finalized(self) -> None:
        with self._lock:
            self._finalized = True
            self.updated_at = time.time()


class ComponentSessionManager:
    """持有活动组件会话，超时清理，读写用一个锁。"""

    def __init__(self, ttl_seconds: float = SESSION_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._sessions: Dict[str, ComponentSession] = {}
        self._lock = threading.RLock()

    def create(
        self, plugin_id: str, component_id: str, meta: Dict[str, Any]
    ) -> ComponentSession:
        session = ComponentSession(plugin_id, component_id, meta)
        with self._lock:
            self._sessions[session.session_id] = session
        self._cleanup()
        return session

    def get(self, session_id: str) -> Optional[ComponentSession]:
        self._cleanup()
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return None
        session.touch()
        return session

    def drop(self, session_id: str) -> Optional[ComponentSession]:
        with self._lock:
            return self._sessions.pop(session_id, None)

    def list_active(self) -> List[ComponentSession]:
        self._cleanup()
        with self._lock:
            return sorted(
                self._sessions.values(), key=lambda session: session.created_at
            )

    def _cleanup(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if now - session.updated_at > self._ttl
            ]
            for session_id in expired:
                self._sessions.pop(session_id, None)
