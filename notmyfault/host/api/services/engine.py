from __future__ import annotations

from collections import deque
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, Protocol

from notmyfault.application_paths import ApplicationPaths
from notmyfault.config import ConfigValidationError, SignedConfigStore
from notmyfault.core.logging import get_latest_log
from notmyfault.core.rules import get_rule_events
from notmyfault.core.run_history import RunHistory
from notmyfault.host.api.ports import EngineControlPort
from notmyfault.security.security import detect_security_mode


class SessionCleanupPort(Protocol):
    def drop_all(self) -> None: ...


@dataclass(frozen=True, slots=True)
class EngineServiceError(Exception):
    kind: str
    body: Dict[str, Any]


class EngineService:
    def __init__(
        self,
        engine: EngineControlPort,
        store: SignedConfigStore,
        paths: ApplicationPaths,
        history: RunHistory,
        extension_sessions: SessionCleanupPort,
        process_id: Callable[[], int] = os.getpid,
    ) -> None:
        self._engine = engine
        self._store = store
        self._paths = paths
        self._history = history
        self._extension_sessions = extension_sessions
        self._process_id = process_id

    def start(self) -> Dict[str, Any]:
        current_state = self._engine.engine_state
        if current_state in ("running", "starting"):
            return {
                "ok": True,
                "running": self._engine.engine_running,
                "engine_state": current_state,
                "api_alive": True,
                "message": (
                    "already_running"
                    if current_state == "running"
                    else "already_starting"
                ),
            }
        if self._engine.start_engine() is False:
            raise EngineServiceError(
                "conflict",
                {
                    "ok": False,
                    "running": self._engine.engine_running,
                    "engine_state": self._engine.engine_state,
                    "message": "engine_stopping",
                },
            )
        return {
            "ok": True,
            "running": self._engine.engine_running,
            "engine_state": self._engine.engine_state,
            "api_alive": True,
        }

    def stop(self) -> Dict[str, Any]:
        self._drop_sessions()
        stopped = self._engine.stop_engine()
        return {
            "ok": True,
            "stopped": stopped,
            "stopping": not stopped,
            "engine_state": self._engine.engine_state,
            "api_alive": True,
        }

    def shutdown(self) -> Dict[str, Any]:
        self._drop_sessions()
        self._engine.request_process_shutdown()
        return {"ok": True, "message": "shutting_down"}

    def status(self) -> Dict[str, Any]:
        rules = self._load_rules()
        trigger_types = {
            event.get("type")
            for rule in rules
            if isinstance(rule, dict)
            for event in get_rule_events(rule)
            if event.get("type")
        }
        action_types: set[str] = set()
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            actions = rule.get("actions", [])
            if not isinstance(actions, list):
                continue
            action_types.update(
                action.get("type")
                for action in actions
                if isinstance(action, dict) and action.get("type")
            )
        return {
            "running": self._engine.engine_running,
            "api_alive": True,
            "engine_running": self._engine.engine_running,
            "engine_state": self._engine.engine_state,
            "pid": self._process_id(),
            "rules_count": len(rules),
            "triggers_count": len(trigger_types),
            "actions_count": len(action_types),
            "security_mode": detect_security_mode().value,
            "last_error": self._engine.last_error,
            "scheduler": self._scheduler_summary(),
        }

    def platform_status(self) -> Dict[str, Any]:
        from notmyfault.platform.capabilities import probe_capabilities

        if sys.platform.startswith("linux"):
            from notmyfault.platform.linux_support import capability_report

            return capability_report()
        return {
            "platform": "windows" if sys.platform == "win32" else sys.platform,
            "desktop": None,
            "session_type": None,
            "capabilities": probe_capabilities(),
            "limitations": {},
        }

    def diagnostics(self) -> Dict[str, Any]:
        current_engine = self._engine.current_engine
        if current_engine is None:
            return {
                "uptime_seconds": 0,
                "plugins": {},
                "rules": {},
                "actions": {},
            }
        return current_engine.get_diagnostics()

    def runs(self, limit: int) -> Dict[str, Any]:
        safe_limit = min(max(int(limit), 1), 1000)
        return {"runs": self._history.list_runs(safe_limit)}

    def run_detail(self, run_id: str) -> Dict[str, Any] | None:
        return self._history.get_run(run_id)

    def cancel_run(self, run_id: str) -> Dict[str, Any]:
        current_engine = self._engine.current_engine
        if current_engine is None or not current_engine.cancel_run(run_id):
            raise EngineServiceError(
                "not_found",
                {"ok": False, "error": "这次运行已经结束或不存在"},
            )
        return {"ok": True, "message": "已请求停止这次运行"}

    def logs(self, lines: int) -> Dict[str, Any]:
        safe_lines = min(max(int(lines), 1), 2000)
        log_path = get_latest_log(str(self._paths.logs_dir))
        if not log_path:
            return {"lines": [], "total": 0}
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as file:
                tail: deque[str] = deque(maxlen=safe_lines)
                total = 0
                for line in file:
                    total += 1
                    tail.append(line.rstrip("\n"))
        except FileNotFoundError:
            return {"lines": [], "total": 0}
        return {
            "lines": list(tail),
            "total": total,
        }

    def _load_rules(self) -> list[Dict[str, Any]]:
        try:
            return self._store.load_verified_rules()
        except ConfigValidationError:
            return []

    def _scheduler_summary(self) -> Dict[str, Any]:
        current_engine = self._engine.current_engine
        if current_engine is None:
            return {"running": 0, "queued": 0, "rules": {}}
        rules = current_engine.scheduler_stats()
        return {
            "running": sum(item["running"] for item in rules.values()),
            "queued": sum(item["queued"] for item in rules.values()),
            "rules": rules,
        }

    def _drop_sessions(self) -> None:
        self._extension_sessions.drop_all()
