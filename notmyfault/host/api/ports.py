from __future__ import annotations

from typing import Any, Dict, List, Protocol


class DesktopElementPort(Protocol):
    async def capture(self, delay_value: Any) -> Dict[str, Any]: ...

    async def check(self, selector: Any) -> Dict[str, Any]: ...


class PluginRegistryPort(Protocol):
    def load(self, url: str) -> Dict[str, Any]: ...

    def download(
        self,
        url: str,
        package_name: str,
        version: str,
    ) -> tuple[bytes, Dict[str, Any]]: ...


class ActiveEnginePort(Protocol):
    @property
    def actions_meta(self) -> Dict[str, Dict[str, Any]]: ...

    @property
    def triggers_meta(self) -> Dict[str, Dict[str, Any]]: ...

    @property
    def extensions(self) -> Any: ...

    @property
    def admin_authorization_mode(self) -> str: ...

    def scheduler_stats(self) -> Dict[str, Dict[str, int]]: ...

    def get_diagnostics(self) -> Dict[str, Any]: ...

    def run_manual_rule_snapshot(
        self,
        rule: Dict[str, Any],
        rule_index: int = -1,
        trigger_payloads: Dict[str, Dict[str, Any]] | None = None,
        event_payload: Dict[str, Any] | None = None,
        step_outputs: Dict[str, Any] | None = None,
        start_step_id: str = "",
        end_step_id: str = "",
        test_assertions: List[Dict[str, Any]] | None = None,
    ) -> tuple[bool, str, str]: ...

    def cancel_run(self, run_id: str) -> bool: ...

    def component(self, plugin_id: str, component_id: str) -> Any: ...

    def extension_handler(self, plugin_id: str, command_id: str) -> Any: ...


class EngineControlPort(Protocol):
    @property
    def engine_running(self) -> bool: ...

    @property
    def engine_state(self) -> str: ...

    @property
    def last_error(self) -> str | None: ...

    @property
    def current_engine(self) -> ActiveEnginePort | None: ...

    def start_engine(self) -> bool: ...

    def stop_engine(self) -> bool: ...

    def request_process_shutdown(self, force_after: float = 10) -> None: ...
