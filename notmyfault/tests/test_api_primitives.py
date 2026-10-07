from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
import asyncio
import json
import sys
import threading

import pytest

from notmyfault.host.api.plugin_installation import (
    PendingPreviewStore,
    PluginBackupStore,
    PluginFileSystem,
    PluginInstallCleanupError,
    PluginInstallRollbackError,
    PluginInstallTransaction,
)
from notmyfault.host.api.events import EventBroker
from notmyfault.host.api.services.rule_runs import RuleRunService, RuleRunServiceError
from notmyfault.core.value_codec import encode_value
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


@pytest.mark.parametrize("failure_stage", ["replace", "commit"])
@pytest.mark.parametrize("installation", ["new", "update", "renamed"])
def test_plugin_install_transaction_restores_replaced_directory(
    tmp_path, failure_stage, installation,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "version.txt").write_text("new", encoding="utf-8")
    root = tmp_path / "plugins"
    root.mkdir()
    destination = root / "demo"
    old_destination = root / ("old_demo" if installation == "renamed" else "demo")
    old_backup = old_destination.with_name(old_destination.name + ".nmf-backup")
    if installation != "new":
        old_destination.mkdir()
        (old_destination / "version.txt").write_text("old", encoding="utf-8")
        old_backup.mkdir()
        (old_backup / "version.txt").write_text("older", encoding="utf-8")

    class FailingFileSystem(PluginFileSystem):
        def replace(self, source: Path, destination: Path) -> None:
            if failure_stage == "replace" and source.name.startswith(".demo-staging"):
                raise OSError("injected replace failure")
            super().replace(source, destination)

    commits = []

    def commit(path: Path) -> None:
        commits.append(path)
        assert (path / "version.txt").read_text(encoding="utf-8") == "new"
        raise OSError("injected commit failure")

    file_system = FailingFileSystem()
    transaction = PluginInstallTransaction(file_system)

    with pytest.raises(OSError, match=f"injected {failure_stage} failure"):
        transaction.install_tree(
            source,
            destination,
            obsolete_destinations=(old_destination,) if installation == "renamed" else (),
            commit=commit,
        )

    assert commits == ([destination] if failure_stage == "commit" else [])
    if installation != "new":
        assert (old_destination / "version.txt").read_text(encoding="utf-8") == "old"
        assert (old_backup / "version.txt").read_text(encoding="utf-8") == "older"
    if installation != "update":
        assert not destination.exists()
    assert list(root.glob("*.nmf-backup")) == ([old_backup] if installation != "new" else [])
    assert list(root.glob(".*-staging-*")) == []
    assert list(root.glob(".*-retained-*")) == []


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_plugin_install_transaction_commits_before_old_backup_cleanup(tmp_path, cleanup_failure) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "plugins" / "demo"
    backup = destination.with_name("demo.nmf-backup")
    source.mkdir()
    destination.mkdir(parents=True)
    backup.mkdir()
    (source / "version.txt").write_text("new", encoding="utf-8")
    (destination / "version.txt").write_text("old", encoding="utf-8")
    (backup / "version.txt").write_text("older", encoding="utf-8")
    commits = []

    class CleanupFileSystem(PluginFileSystem):
        def remove_tree(self, path: Path) -> None:
            if "-retained-" in path.name:
                assert commits == [destination]
                if cleanup_failure:
                    raise PermissionError("injected cleanup failure")
            super().remove_tree(path)

    transaction = PluginInstallTransaction(CleanupFileSystem())
    if cleanup_failure:
        with pytest.raises(PluginInstallCleanupError, match="injected cleanup failure"):
            transaction.install_tree(source, destination, commit=commits.append)
    else:
        assert transaction.install_tree(source, destination, commit=commits.append) == (backup,)

    assert commits == [destination]
    assert (destination / "version.txt").read_text(encoding="utf-8") == "new"
    assert (backup / "version.txt").read_text(encoding="utf-8") == "old"
    retained = list(destination.parent.glob(".*-retained-*"))
    assert len(retained) == (1 if cleanup_failure else 0)
    if retained:
        assert (retained[0] / "version.txt").read_text(encoding="utf-8") == "older"


@pytest.mark.parametrize("rollback_failure", ["remove", "restore"])
def test_plugin_install_transaction_reports_failed_restore(tmp_path, rollback_failure) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "plugins" / "demo"
    backup = destination.with_name("demo.nmf-backup")
    source.mkdir()
    destination.mkdir(parents=True)
    (source / "version.txt").write_text("new", encoding="utf-8")
    (destination / "version.txt").write_text("old", encoding="utf-8")

    class FailingRestoreFileSystem(PluginFileSystem):
        def remove_tree(self, path: Path) -> None:
            if rollback_failure == "remove" and path == destination:
                raise PermissionError("injected remove failure")
            super().remove_tree(path)

        def replace(self, source: Path, target: Path) -> None:
            if rollback_failure == "restore" and source == backup:
                raise OSError("injected restore failure")
            super().replace(source, target)

    def commit(path: Path) -> None:
        raise OSError("injected commit failure")

    transaction = PluginInstallTransaction(FailingRestoreFileSystem())

    with pytest.raises(PluginInstallRollbackError, match=f"injected {rollback_failure} failure") as excinfo:
        transaction.install_tree(source, destination, commit=commit)

    assert str(excinfo.value.__cause__) == "injected commit failure"
    assert (backup / "version.txt").read_text(encoding="utf-8") == "old"
    if rollback_failure == "remove":
        assert (destination / "version.txt").read_text(encoding="utf-8") == "new"
    else:
        assert not destination.exists()
    assert list(destination.parent.glob(".*-staging-*")) == []


@pytest.mark.parametrize("same_id", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_plugin_install_transaction_restores_retained_backup(
    tmp_path, same_id, cleanup_fails,
) -> None:
    root = tmp_path / "plugins"
    active = root / ("old_id" if same_id else "new_id")
    backup = root / "old_id.nmf-backup"
    destination = root / "old_id"
    active.mkdir(parents=True)
    backup.mkdir()
    (active / "version.txt").write_text("new", encoding="utf-8")
    (backup / "version.txt").write_text("old", encoding="utf-8")

    class CleanupFileSystem(PluginFileSystem):
        def remove_tree(self, path: Path) -> None:
            if cleanup_fails and "-failed-" in path.name and path.exists():
                raise OSError("injected cleanup failure")
            super().remove_tree(path)

    committed = []
    transaction = PluginInstallTransaction(CleanupFileSystem())
    if cleanup_fails:
        with pytest.raises(PluginInstallCleanupError, match="旧版已恢复"):
            transaction.restore_backup(backup, destination, active, commit=committed.append)
        retained = list(root.glob(".*-failed-*"))
        assert len(retained) == 1
        assert (retained[0] / "version.txt").read_text(encoding="utf-8") == "new"
    else:
        assert transaction.restore_backup(
            backup, destination, active, commit=committed.append,
        ) == destination
        assert list(root.glob(".*-failed-*")) == []
    assert committed == [destination]
    assert (destination / "version.txt").read_text(encoding="utf-8") == "old"
    if not same_id:
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
    def record_async(self, event) -> None:
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

        for index in range(subscription.queue.maxsize + 1):
            broker.publish("progress", {"index": index})
        await asyncio.sleep(0)

        assert await subscription.queue.get() is None

    asyncio.run(exercise())


def test_event_broker_close_ends_open_subscribers() -> None:
    async def exercise() -> None:
        broker = EventBroker(_EventHistory())
        subscription = broker.subscribe()

        broker.close()

        assert await asyncio.wait_for(subscription.queue.get(), timeout=1) is None

    asyncio.run(exercise())


def test_event_broker_subscribe_after_close_ends_immediately() -> None:
    async def exercise() -> None:
        broker = EventBroker(_EventHistory())
        broker.close()

        subscription = broker.subscribe()

        assert subscription.queue.get_nowait() is None

    asyncio.run(exercise())


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/rules/0/run"),
    ("POST", "/api/rules/validate"),
    ("PUT", "/api/rules"),
    ("POST", "/api/rules/draft/ai/stream"),
    ("POST", "/api/plugins/demo/extensions/commands/read/invoke"),
])
def test_rule_run_route_rejects_test_data_over_one_mib(tmp_path, method, path) -> None:
    env = make_api_env(tmp_path)
    response = env.client.request(
        method, path,
        headers={**env.headers, "Content-Type": "application/json"},
        content=b'"' + b"x" * (1024 * 1024) + b'"',
    )

    assert response.status_code == 413
    assert response.json()["ok"] is False


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
        "condition": {
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


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("field,value", [
    ("step_id", []), ("step_id", {}), ("step_id", 1),
    ("operator", []), ("operator", {}), ("operator", True),
    ("expected", float("nan")), ("expected", float("inf")),
    ("expected", float("-inf")), ("expected", {"nested": [float("nan")]}),
    ("expected", "deep-array"),
    ("expected", "parser-integer"),
])
def test_rule_run_route_rejects_invalid_assertions_before_execution(tmp_path, typed, field, value):
    env = make_api_env(tmp_path)
    rule = {
        "name": "测试检查项",
        "condition": {"type": "manual", "binding_id": "t_manual01", "params": {}},
        "actions": [{"type": "target", "binding_id": "a_target001", "params": {}}],
    }
    assert env.store.save_rules([rule])
    calls = []
    env.runner.current_engine = SimpleNamespace(
        actions_meta={"target": {"outputs": []}},
        triggers_meta={"manual": {"outputs": []}},
        run_manual_rule_snapshot=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or (True, "已执行", "run-test")
        ),
    )
    headers = {**env.headers, **({"X-NMF-Value-Encoding": "typed-v1"} if typed else {})}
    if field == "expected" and value == "deep-array":
        value = None
        for _ in range(200):
            value = [value]
    assertion = {
        "step_id": "a_target001",
        "operator": "equals" if field == "expected" else "exists",
        "path": [], field: value,
    }
    raw_body = json.dumps({"test_assertions": [assertion]})
    if field == "expected" and value == "parser-integer":
        digit_limit = sys.get_int_max_str_digits()
        if not digit_limit:
            pytest.skip("当前 Python 未启用整数文本转换上限")
        raw_body = raw_body.replace(json.dumps(value), "9" * (digit_limit + 1))
    response = env.client.post(
        "/api/rules/0/run", headers={**headers, "Content-Type": "application/json"},
        content=raw_body,
    )
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert calls == []
    expected = {
        "flag": True, "number": 1.25, "integer": 2**80,
        "nested": [None, {"text": "保留数据"}],
        **({"decimal": Decimal("1.234567890123456789")} if typed else {}),
    }
    valid_body = {"test_assertions": [
        {"step_id": "a_target001", "operator": "exists", "path": []},
        {"step_id": "a_target001", "operator": "equals", "path": [], "expected": expected},
    ]}
    response = env.client.post(
        "/api/rules/0/run", headers=headers,
        json=encode_value(valid_body) if typed else valid_body,
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][1]["test_assertions"] == valid_body["test_assertions"]


def test_rule_run_service_allows_assertions_in_selected_elif_but_not_starting_inside_it():
    rule = {"rule_id": "r_target01", "name": "ELIF检查", "condition": {"type": "signal", "binding_id": "t_signal01", "params": {}},
            "actions": [{"type": "if", "binding_id": "a_branch01", "condition": {"op": "is_true", "left": False}, "then": [], "else": [],
                         "elif": [{"condition": {"op": "is_true", "left": True}, "then": [{"type": "target", "binding_id": "a_target01", "params": {}}]}]}]}
    calls = []
    engine = SimpleNamespace(actions_meta={"target": {"outputs": []}}, triggers_meta={"signal": {"outputs": []}},
                             run_manual_rule_snapshot=lambda *args, **kwargs: (calls.append(kwargs) or (True, "已执行", "run_branch")))
    service = RuleRunService(SimpleNamespace(current_engine=engine), SimpleNamespace(load_verified_rules=lambda: [rule]))
    body = {"test_assertions": [{"step_id": "a_target01", "path": [], "operator": "exists"}]}
    assert service.run(0, body)["ok"]
    assert calls[0]["test_assertions"] == body["test_assertions"]
    with pytest.raises(RuleRunServiceError) as invalid:
        service.run(0, {"start_step_id": "a_target01"})
    assert invalid.value.status_code == 400


def test_rule_run_service_checks_missing_and_invalid_reference_payloads() -> None:
    rule = {
        "name": "引用检查",
        "condition": {
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
