from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from notmyfault.application_paths import ApplicationPaths
from notmyfault.config import SignedConfigStore
from notmyfault.host.api.auth import ApiTokenStore
from notmyfault.host.api.events import EventBroker
from notmyfault.host.api.plugin_installation import (
    PendingPreviewStore,
    PluginFileSystem,
    PluginTemporaryStorage,
)
from notmyfault.host.api_server import create_api_server
from notmyfault.host.plugin_registry import PluginRegistryClient
from notmyfault.core.run_history import RunHistory
from notmyfault.security.api_key_store import KeyStoreStatus


API_TOKEN = "a" * 64


class StaticRulesStore:
    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self._rules = rules
        directory = Path(tempfile.mkdtemp(prefix="notmyfault-test-rules-"))
        self.rules_path = str(directory / "rules.json")
        self.plugin_manifest_path = str(directory / "plugin_manifest.json")

    def load_verified_rules(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._rules)


def create_test_engine(config, *args, **kwargs):
    from notmyfault.core.engine import AutomationEngine

    rules_store = kwargs.pop(
        "rules_store", StaticRulesStore(config.get("rules", []))
    )
    return AutomationEngine(config, *args, rules_store=rules_store, **kwargs)


class FakeRunner:
    def __init__(self) -> None:
        self.engine_running = False
        self.engine_state = "stopped"
        self.last_error = None
        self.current_engine = None
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.start_result = True

    def start_engine(self) -> bool:
        self.start_calls += 1
        if self.start_result:
            self.engine_running = True
            self.engine_state = "running"
        return self.start_result

    def stop_engine(self) -> bool:
        self.stop_calls += 1
        self.engine_running = False
        self.engine_state = "stopped"
        return True

    def request_process_shutdown(self, force_after: float = 10) -> None:
        self.shutdown_calls += 1


class FakeKeyStore:
    def __init__(self) -> None:
        self.value: str | None = None
        self.status = KeyStoreStatus.ABSENT

    def save_api_key(self, value: str) -> None:
        self.value = value.strip()
        self.status = KeyStoreStatus.STORED

    def load_api_key(self) -> str | None:
        return self.value

    def delete_api_key(self) -> None:
        self.value = None
        self.status = KeyStoreStatus.ABSENT

    def api_key_status(self) -> KeyStoreStatus:
        return self.status


class FakeDesktopElements:
    async def capture(self, delay_value: Any) -> dict[str, Any]:
        return {"ok": True, "selector": {"control_type": "Button"}}

    async def check(self, selector: Any) -> dict[str, Any]:
        return {"ok": True, "matched": bool(selector)}


def make_paths(
    tmp_path: Path,
    package_root: Path | None = None,
) -> ApplicationPaths:
    package = package_root or Path(__file__).resolve().parents[1]
    return ApplicationPaths(
        config_dir=tmp_path / "config",
        package_root=package,
        project_root=tmp_path,
    )


def make_store(paths: ApplicationPaths) -> SignedConfigStore:
    store = SignedConfigStore(paths)
    assert store.save_config({})
    assert store.save_rules([])
    return store


def make_api_env(
    tmp_path: Path,
    runner: FakeRunner | None = None,
    package_root: Path | None = None,
    **dependencies: Any,
) -> SimpleNamespace:
    paths = make_paths(tmp_path, package_root)
    store = make_store(paths)
    token_store = ApiTokenStore(
        paths.api_token_file,
        token=API_TOKEN,
        writer=lambda token: paths.api_token_file.write_text(
            token, encoding="utf-8"
        ),
    )
    active_runner = runner or FakeRunner()
    ai_key_store = dependencies.pop("ai_key_store", FakeKeyStore())
    plugin_file_system = dependencies.pop("plugin_file_system", None)
    pending_previews = dependencies.pop("pending_previews", None)
    plugin_temporary_storage = dependencies.pop("plugin_temporary_storage", None)
    desktop_elements = dependencies.pop("desktop_elements", FakeDesktopElements())
    plugin_registry = dependencies.pop("plugin_registry", PluginRegistryClient())
    run_history = dependencies.pop(
        "run_history", RunHistory(str(paths.run_history_file))
    )
    event_broker = dependencies.pop("event_broker", EventBroker(run_history))
    server = create_api_server(
        active_runner,
        store,
        paths,
        token_store=token_store,
        ai_key_store=ai_key_store,
        plugin_file_system=(
            plugin_file_system
            if plugin_file_system is not None
            else PluginFileSystem()
        ),
        pending_previews=(
            pending_previews
            if pending_previews is not None
            else PendingPreviewStore()
        ),
        plugin_temporary_storage=(
            plugin_temporary_storage
            if plugin_temporary_storage is not None
            else PluginTemporaryStorage()
        ),
        desktop_elements=desktop_elements,
        plugin_registry=plugin_registry,
        run_history=run_history,
        event_broker=event_broker,
        **dependencies,
    )
    return SimpleNamespace(
        server=server,
        runner=active_runner,
        store=store,
        paths=paths,
        token_store=token_store,
        ai_key_store=ai_key_store,
        run_history=run_history,
        event_broker=event_broker,
        client=TestClient(server.app),
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    )
