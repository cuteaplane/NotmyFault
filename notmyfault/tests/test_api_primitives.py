from pathlib import Path
from types import SimpleNamespace
import asyncio
import json
import threading

import pytest

from notmyfault.host.api.plugin_installation import (
    PendingPreviewStore,
    PluginBackupStore,
    PluginFileSystem,
    PluginInstallTransaction,
)
from notmyfault.host.api.services.settings import (
    SettingsService,
    SettingsServiceError,
)
from notmyfault.host.api.services.engine import EngineService
from notmyfault.host.api.events import EventBroker
from notmyfault.host.api.routes_engine import create_engine_router
from notmyfault.host.api.services.rule_runs import RuleRunService, RuleRunServiceError
from notmyfault.host.api.services.ai_drafting import AIDraftingService
from notmyfault.security.api_key_store import KeyStoreStatus
from notmyfault.tests.api_support import make_api_env


def test_pending_preview_store_purges_expired_directory() -> None:
    removed: list[Path] = []
    now = [100.0]
    store = PendingPreviewStore(
        clock=lambda: now[0],
        remove_tree=removed.append,
        ttl_seconds=30,
    )
    store["token"] = {
        "created_at": 100.0,
        "extract_dir": "preview-directory",
    }

    now[0] = 131.0
    store.purge_expired()

    assert "token" not in store
    assert removed == [Path("preview-directory")]


def test_pending_preview_store_discard_cleans_directory() -> None:
    removed: list[Path] = []
    store = PendingPreviewStore(remove_tree=removed.append)
    store["token"] = {
        "created_at": 100.0,
        "extract_dir": "preview-directory",
    }

    store.discard("token")
    store.discard("token")

    assert "token" not in store
    assert removed == [Path("preview-directory")]


def test_plugin_install_transaction_restores_replaced_directory() -> None:
    class FailingFileSystem:
        def __init__(self) -> None:
            self.replacements: list[tuple[Path, Path]] = []
            self.removals: list[Path] = []

        def make_dirs(self, path: Path) -> None:
            return None

        def remove_tree(self, path: Path) -> None:
            self.removals.append(path)

        def discard_tree(self, path: Path) -> None:
            self.removals.append(path)

        def copy_tree(self, source: Path, destination: Path) -> None:
            return None

        def exists(self, path: Path) -> bool:
            return path == Path("plugins/demo")

        def replace(self, source: Path, destination: Path) -> None:
            self.replacements.append((source, destination))
            if source.name.startswith(".demo-staging"):
                raise OSError("injected replace failure")

    file_system = FailingFileSystem()
    transaction = PluginInstallTransaction(file_system)

    with pytest.raises(OSError, match="injected replace failure"):
        transaction.install_tree(Path("source"), Path("plugins/demo"))

    backup = Path("plugins/demo.nmf-backup")
    assert file_system.replacements[0] == (Path("plugins/demo"), backup)
    assert file_system.replacements[-1] == (backup, Path("plugins/demo"))


def test_plugin_install_transaction_restores_retained_backup(tmp_path) -> None:
    root = tmp_path / "plugins"
    active = root / "new_id"
    backup = root / "old_id.nmf-backup"
    destination = root / "old_id"
    active.mkdir(parents=True)
    backup.mkdir()
    (active / "version.txt").write_text("new", encoding="utf-8")
    (backup / "version.txt").write_text("old", encoding="utf-8")

    restored = PluginInstallTransaction(PluginFileSystem()).restore_backup(
        backup,
        destination,
        active,
    )

    assert restored == destination
    assert (destination / "version.txt").read_text(encoding="utf-8") == "old"
    assert not active.exists()
    assert not backup.exists()


def test_plugin_backup_restore_failure_puts_updated_plugin_back(tmp_path) -> None:
    root = tmp_path / "plugins"
    active = root / "new_id"
    backup = root / "old_id.nmf-backup"
    destination = root / "old_id"
    active.mkdir(parents=True)
    backup.mkdir()
    (active / "version.txt").write_text("new", encoding="utf-8")
    (backup / "version.txt").write_text("old", encoding="utf-8")

    class FailingRestoreFileSystem(PluginFileSystem):
        def replace(self, source: Path, target: Path) -> None:
            if source == backup and target == destination:
                raise OSError("injected restore failure")
            super().replace(source, target)

    transaction = PluginInstallTransaction(FailingRestoreFileSystem())

    with pytest.raises(OSError, match="injected restore failure"):
        transaction.restore_backup(backup, destination, active)

    assert (active / "version.txt").read_text(encoding="utf-8") == "new"
    assert (backup / "version.txt").read_text(encoding="utf-8") == "old"
    assert list(root.glob(".*-failed-*")) == []


def test_backup_store_prefers_backup_named_after_current_plugin(tmp_path) -> None:
    root = tmp_path / "plugins" / "actions"
    current = root / "new_id"
    old_backup = root / "old_id.nmf-backup"
    latest_backup = root / "new_id.nmf-backup"
    for path, plugin_id, version in (
        (current, "new_id", 3),
        (old_backup, "old_id", 1),
        (latest_backup, "new_id", 2),
    ):
        path.mkdir(parents=True)
        (path / "action.json").write_text(
            json.dumps(
                {
                    "id": plugin_id,
                    "package_name": "com.test.same_package",
                    "version_code": version,
                }
            ),
            encoding="utf-8",
        )

    retained = PluginBackupStore(
        tmp_path / "plugins",
        PluginFileSystem(),
    ).retained()

    assert len(retained) == 1
    assert retained[0].backup_path == latest_backup
    assert retained[0].active_path == current


class _SettingsStore:
    config_path = "config.json"

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.saved: list[dict] = []

    def load_verified_config(self) -> dict:
        return dict(self.config)

    def save_config(self, config: dict) -> bool:
        self.saved.append(config)
        self.config = config
        return True

    def inspect_security(self) -> dict:
        return {"status": "ok"}

    def approve_current_files(self) -> None:
        return None


def test_settings_service_updates_injected_store() -> None:
    store = _SettingsStore({"settings": {}})
    engine = SimpleNamespace(current_engine=None)
    service = SettingsService(store, engine, platform_name="nt")

    result = service.update_admin_authorization("engine_start")

    assert result == {
        "ok": True,
        "mode": "engine_start",
        "effective_mode": None,
        "restart_required": False,
    }
    assert store.saved[-1]["settings"]["admin_authorization_mode"] == "engine_start"


def test_settings_service_rejects_platform_specific_mode() -> None:
    service = SettingsService(
        _SettingsStore({"settings": {}}),
        SimpleNamespace(current_engine=None),
        platform_name="posix",
    )

    with pytest.raises(SettingsServiceError) as error:
        service.update_admin_authorization("engine_start")

    assert error.value.kind == "invalid"


class _SessionStore:
    def __init__(self) -> None:
        self.dropped = 0

    def drop_all(self) -> None:
        self.dropped += 1


class _History:
    def list_runs(self, limit: int) -> list:
        return [{"limit": limit}]

    def get_run(self, run_id: str):
        return {"run_id": run_id} if run_id == "known" else None


def test_engine_service_stop_cleans_sessions() -> None:
    component_sessions = _SessionStore()
    extension_sessions = _SessionStore()
    engine = SimpleNamespace(
        engine_running=False,
        engine_state="stopped",
        last_error=None,
        current_engine=None,
        stop_engine=lambda: True,
    )
    paths = SimpleNamespace(logs_dir=Path("logs"))
    service = EngineService(
        engine,
        SimpleNamespace(load_verified_rules=lambda: []),
        paths,
        _History(),
        component_sessions,
        extension_sessions,
        SimpleNamespace(),
    )

    result = service.stop()

    assert result["stopped"] is True
    assert component_sessions.dropped == 1
    assert extension_sessions.dropped == 1


def test_engine_service_uses_injected_rule_store_for_status() -> None:
    rules = [{"event": {"type": "clipboard"}, "actions": [{"type": "notify"}]}]
    engine = SimpleNamespace(
        engine_running=False,
        engine_state="stopped",
        last_error=None,
        current_engine=None,
    )
    service = EngineService(
        engine,
        SimpleNamespace(load_verified_rules=lambda: rules),
        SimpleNamespace(logs_dir=Path("logs")),
        _History(),
        _SessionStore(),
        _SessionStore(),
        SimpleNamespace(),
        process_id=lambda: 123,
    )

    result = service.status()

    assert result["pid"] == 123
    assert result["rules_count"] == 1
    assert result["triggers_count"] == 1
    assert result["actions_count"] == 1


class _EventHistory:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        self.events.append(event)


def test_event_broker_delivers_from_worker_thread() -> None:
    async def exercise() -> None:
        history = _EventHistory()
        broker = EventBroker(history)
        subscription = broker.subscribe()

        worker = threading.Thread(
            target=lambda: broker.publish("changed", {"value": 1})
        )
        worker.start()
        worker.join()
        event = await asyncio.wait_for(subscription.queue.get(), timeout=1)

        assert event["type"] == "changed"
        assert event["data"] == {"value": 1}
        assert history.events[0]["type"] == "changed"

    asyncio.run(exercise())


def test_event_broker_closes_slow_subscriber() -> None:
    async def exercise() -> None:
        broker = EventBroker(_EventHistory())
        subscription = broker.subscribe()

        for index in range(201):
            broker.publish("progress", {"index": index})
        await asyncio.sleep(0)

        assert await subscription.queue.get() is None

    asyncio.run(exercise())


def test_sse_route_keeps_event_format_and_unsubscribes() -> None:
    class Events:
        def __init__(self) -> None:
            self.unsubscribed = False

        def subscribe(self):
            queue = asyncio.Queue()
            queue.put_nowait({"type": "changed", "data": {"value": 1}})
            return SimpleNamespace(queue=queue)

        def unsubscribe(self, subscription) -> None:
            self.unsubscribed = True

    class Request:
        async def is_disconnected(self) -> bool:
            return False

    async def exercise() -> None:
        events = Events()
        router = create_engine_router(SimpleNamespace(), events)
        endpoint = next(
            route.endpoint for route in router.routes if route.path == "/api/events"
        )
        response = await endpoint(Request())
        iterator = response.body_iterator

        assert await anext(iterator) == "event: changed\n"
        assert await anext(iterator) == 'data: {"value": 1}\n\n'
        await iterator.aclose()
        assert events.unsubscribed is True

    asyncio.run(exercise())


def test_rule_run_service_reads_rules_from_injected_store() -> None:
    rule = {
        "name": "通知规则",
        "event": {"type": "usb_insert", "params": {}},
        "actions": [{"type": "open_url", "params": {}}],
    }
    calls: list[tuple] = []
    active_engine = SimpleNamespace(
        actions_meta={},
        triggers_meta={},
        run_manual_rule_snapshot=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or (True, "已执行", "run-1")
        ),
    )
    service = RuleRunService(
        SimpleNamespace(current_engine=active_engine),
        SimpleNamespace(load_verified_rules=lambda: [rule]),
    )

    result = service.run(0, None)

    assert result == {
        "ok": True,
        "message": "已执行",
        "run_id": "run-1",
        "action_count": 1,
    }
    assert calls[0][0][0] == rule


def test_rule_run_route_rejects_test_data_over_one_mib(tmp_path) -> None:
    env = make_api_env(tmp_path)
    response = env.client.post(
        "/api/rules/0/run",
        headers={**env.headers, "Content-Type": "application/json"},
        content=b'"' + b"x" * (1024 * 1024) + b'"',
    )

    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "测试数据超过 1 MiB 上限"}


def test_rule_run_service_forwards_scoped_test_context() -> None:
    rule = {
        "name": "局部运行",
        "event": {
            "type": "hotkey",
            "binding_id": "t_hot001",
            "params": {},
        },
        "actions": [
            {
                "type": "source",
                "binding_id": "a_source001",
                "params": {},
            },
            {
                "type": "target",
                "binding_id": "a_target001",
                "params": {
                    "upstream": {
                        "$ref": {
                            "scope": "step",
                            "node": "a_source001",
                            "path": ["value"],
                        }
                    },
                    "trigger": {
                        "$ref": {
                            "scope": "trigger",
                            "node": "t_hot001",
                            "path": ["key"],
                        }
                    },
                    "event": {"$ref": {"scope": "event", "path": ["kind"]}},
                },
            },
        ],
    }
    calls = []
    active_engine = SimpleNamespace(
        actions_meta={
            "source": {"outputs": [{"name": "value", "type": "string"}]},
            "target": {"outputs": []},
        },
        triggers_meta={
            "hotkey": {"outputs": [{"name": "key", "type": "string"}]}
        },
        run_manual_rule_snapshot=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or (True, "已执行", "run-2")
        ),
    )
    service = RuleRunService(
        SimpleNamespace(current_engine=active_engine),
        SimpleNamespace(load_verified_rules=lambda: [rule]),
    )
    body = {
        "trigger_payloads": {"t_hot001": {"key": "Ctrl+K"}},
        "event_payload": {"kind": "manual"},
        "step_outputs": {"a_source001": {"value": "ready"}},
        "start_step_id": "a_target001",
        "end_step_id": "a_target001",
        "test_assertions": [
            {
                "step_id": "a_target001",
                "path": [],
                "operator": "exists",
            }
        ],
    }

    result = service.run(0, body)

    assert result == {
        "ok": True,
        "message": "已执行",
        "run_id": "run-2",
        "action_count": 1,
    }
    assert calls[0][1] == body


def test_rule_run_service_checks_missing_and_invalid_reference_payloads() -> None:
    rule = {
        "name": "引用检查",
        "event": {
            "type": "hotkey",
            "binding_id": "t_hot001",
            "params": {},
        },
        "actions": [
            {
                "type": "target",
                "binding_id": "a_target001",
                "params": {
                    "key": {
                        "$ref": {
                            "scope": "trigger",
                            "node": "t_hot001",
                            "path": ["key"],
                        }
                    }
                },
            }
        ],
    }
    active_engine = SimpleNamespace(
        actions_meta={"target": {"outputs": []}},
        triggers_meta={
            "hotkey": {"outputs": [{"name": "key", "type": "string"}]}
        },
        run_manual_rule_snapshot=lambda *args, **kwargs: (True, "已执行", "run-3"),
    )
    service = RuleRunService(
        SimpleNamespace(current_engine=active_engine),
        SimpleNamespace(load_verified_rules=lambda: [rule]),
    )

    with pytest.raises(RuleRunServiceError) as missing:
        service.run(0, {})
    assert missing.value.body["code"] == "missing_test_context"
    assert missing.value.body["required_trigger_ids"] == ["t_hot001"]

    with pytest.raises(RuleRunServiceError) as invalid:
        service.run(0, {"trigger_payloads": {"t_hot001": {"key": 7}}})
    assert invalid.value.body["code"] == "invalid_test_payload"
    assert "不符合触发器输出契约" in invalid.value.body["error"]


class _AIKeyStore:
    @staticmethod
    def api_key_status():
        return KeyStoreStatus.STORED

    @staticmethod
    def load_api_key():
        return "saved-key"

    @staticmethod
    def save_api_key(value):
        return None

    @staticmethod
    def delete_api_key():
        return None


def test_ai_drafting_service_uses_injected_provider() -> None:
    store = _SettingsStore(
        {
            "settings": {
                "ai_drafting": {
                    "enabled": True,
                    "endpoint_url": "",
                    "model": "",
                    "api_format": "chat_completions",
                }
            }
        }
    )
    calls: list[tuple] = []

    def provider(messages, schema, allow_plugin_source):
        calls.append((messages, schema, allow_plugin_source))
        return {"result_type": "assistant_message", "message": "完成"}

    service = AIDraftingService(
        store,
        plugin_schema=lambda: {"triggers": {}, "actions": {}},
        validate_rule=lambda rule: {"ok": True},
        provider=provider,
        key_store=_AIKeyStore,
    )

    result = asyncio.run(
        service.draft({"messages": [{"role": "user", "content": "测试"}]})
    )

    assert result["result_type"] == "assistant_message"
    assert result["message"] == "完成"
    assert calls[0][2] is False
