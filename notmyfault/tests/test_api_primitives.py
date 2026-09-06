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
from notmyfault.host.api.events import EventBroker
from notmyfault.host.api.services.rule_runs import RuleRunService, RuleRunServiceError
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


class _EventHistory:
    def __init__(self) -> None:
        self.events = []
    def record(self, event) -> None:
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


def test_rule_run_route_rejects_test_data_over_one_mib(tmp_path) -> None:
    env = make_api_env(tmp_path)
    response = env.client.post(
        "/api/rules/0/run",
        headers={**env.headers, "Content-Type": "application/json"},
        content=b'"' + b"x" * (1024 * 1024) + b'"',
    )

    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "测试数据超过 1 MiB 上限"}


@pytest.mark.parametrize("upstream,policy,valid", [
    ({"value": "ready"}, None, True),
    ({}, None, False),
    ({}, "default", True),
    ({}, "skip", True),
    ({"value": None}, "default", False),
    ({"value": 7}, "default", False),
    ({"value": ""}, "default", True),
])
def test_rule_run_service_validates_and_forwards_scoped_test_context(upstream, policy, valid) -> None:
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
    if policy is not None:
        reference = rule["actions"][1]["params"]["upstream"]["$ref"]
        reference["on_missing"] = policy
        if policy == "default":
            reference["default"] = "fallback"
    calls = []
    active_engine = SimpleNamespace(
        actions_meta={
            "source": {"outputs": [{"name": "value", "type": "string", "value_type": "text", "required": False}]},
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
        "step_outputs": {"a_source001": upstream},
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

    if not valid:
        with pytest.raises(RuleRunServiceError) as invalid:
            service.run(0, body)
        assert invalid.value.body["code"] == "invalid_test_payload"
        assert calls == []
        return
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
