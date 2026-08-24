from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from notmyfault.host.api.plugin_installation import (
    PendingPreviewStore,
    PluginFileSystem,
)
from notmyfault.host.api.services.plugin_catalog import PluginCatalogService
from notmyfault.host.api.services.plugin_installation import (
    PluginInstallationError,
    PluginInstallationService,
)
from notmyfault.host.plugin_registry import PluginRegistryClient
from notmyfault.tests.api_support import make_api_env
from notmyfault.tests.api_support import make_paths, make_store
from notmyfault.tests.test_api_plugins import build_nmfp, make_meta, post_archive


def test_preview_rejects_directory_tampering_and_deletes_temporary_tree(tmp_path):
    previews = PendingPreviewStore()
    env = make_api_env(tmp_path, pending_previews=previews)
    archive = build_nmfp(tmp_path, make_meta("actions"), "actions", "tamper")
    preview = post_archive(env, "/api/plugins/preview", archive).json()
    token = preview["preview_token"]
    extract_dir = previews[token]["extract_dir"]
    root_path = previews[token]["root_path"]
    with open(f"{root_path}/action.py", "a", encoding="utf-8") as file:
        file.write("\nprint('changed')\n")
    response = env.client.post(
        "/api/plugins/install",
        data={"preview_token": token},
        headers=env.headers,
    )
    assert response.status_code == 409
    assert token not in previews
    assert not Path(extract_dir).exists()


def test_expired_preview_cannot_be_used(tmp_path):
    now = [100.0]
    previews = PendingPreviewStore(clock=lambda: now[0], ttl_seconds=1800)
    env = make_api_env(tmp_path, pending_previews=previews)
    archive = build_nmfp(tmp_path, make_meta("actions"), "actions", "expire")
    token = post_archive(env, "/api/plugins/preview", archive).json()[
        "preview_token"
    ]
    now[0] += 1801
    response = env.client.post(
        "/api/plugins/install",
        data={"preview_token": token},
        headers=env.headers,
    )
    assert response.status_code == 400
    assert token not in previews


def test_changed_package_id_is_backed_up_and_uninstall_removes_backup(tmp_path):
    env = make_api_env(tmp_path)
    first = build_nmfp(tmp_path, make_meta("actions"), "actions", "old")
    assert post_archive(env, "/api/plugins/install", first).status_code == 200
    second = build_nmfp(
        tmp_path,
        make_meta("actions", id="new_id", version_code=2),
        "actions",
        "new",
    )
    assert post_archive(env, "/api/plugins/install", second).status_code == 200
    backup = env.paths.user_plugins_dir / "actions" / "demo_actions.nmf-backup"
    assert backup.is_dir()
    response = env.client.delete(
        "/api/plugins/actions/new_id", headers=env.headers
    )
    assert response.json()["ok"] is True
    assert not backup.exists()


def test_update_after_id_change_keeps_only_latest_backup(tmp_path):
    env = make_api_env(tmp_path)
    first = build_nmfp(tmp_path, make_meta("actions"), "actions", "first-id")
    assert post_archive(env, "/api/plugins/install", first).status_code == 200
    renamed_meta = make_meta("actions", id="new_id", version_code=2)
    second = build_nmfp(tmp_path, renamed_meta, "actions", "second-id")
    assert post_archive(env, "/api/plugins/install", second).status_code == 200
    third = build_nmfp(
        tmp_path,
        make_meta("actions", id="new_id", version_code=3),
        "actions",
        "third-id",
    )

    assert post_archive(env, "/api/plugins/install", third).status_code == 200

    action_root = env.paths.user_plugins_dir / "actions"
    backups = sorted(action_root.glob("*.nmf-backup"))
    assert [path.name for path in backups] == ["new_id.nmf-backup"]
    backup_meta = json.loads(
        (backups[0] / "action.json").read_text(encoding="utf-8")
    )
    assert backup_meta["version_code"] == 2


def test_preview_reports_permission_and_version_differences(tmp_path):
    env = make_api_env(tmp_path)
    first = build_nmfp(
        tmp_path,
        make_meta("actions", permissions=[]),
        "actions",
        "base",
    )
    assert post_archive(env, "/api/plugins/install", first).status_code == 200
    update = build_nmfp(
        tmp_path,
        make_meta("actions", version_code=2, permissions=["network"]),
        "actions",
        "update",
    )
    preview = post_archive(env, "/api/plugins/preview", update).json()
    assert preview["update_diff"]["update"]["kind"] == "upgrade"
    assert preview["update_diff"]["permission_diff"]["added"] == ["network"]


def test_build_output_with_new_risk_keeps_installed_version(tmp_path):
    env = make_api_env(tmp_path)

    def build_hook(root_path, meta):
        if meta.get("version_code") == 2:
            (root_path / "generated.py").write_text(
                "import subprocess\nsubprocess.run(['generated-tool'])\n",
                encoding="utf-8",
            )

    service = PluginInstallationService(
        env.paths,
        env.store,
        PluginCatalogService(env.paths, env.store, env.runner),
        PluginFileSystem(),
        PendingPreviewStore(),
        PluginRegistryClient(),
        build_hook=build_hook,
    )
    base = build_nmfp(tmp_path, make_meta("actions"), "actions", "build-base")
    assert service.install(base.read_bytes())["ok"] is True

    update = build_nmfp(
        tmp_path,
        make_meta("actions", version_code=2),
        "actions",
        "build-update",
    )
    preview = service.preview(update.read_bytes())
    with pytest.raises(PluginInstallationError) as excinfo:
        service.install(None, preview_token=preview["preview_token"])

    assert excinfo.value.status_code == 400
    assert "构建产物引入了新的风险" in excinfo.value.body["error"]
    manifest_path = (
        env.paths.user_plugins_dir / "actions" / "demo_actions" / "action.json"
    )
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["version_code"] == 1


def _write_action(
    path: Path,
    plugin_id: str,
    version_code: int,
    source: str,
) -> None:
    path.mkdir(parents=True)
    meta = make_meta(
        "actions",
        id=plugin_id,
        package_name="com.test.restart_restore",
        version_code=version_code,
        version=f"{version_code}.0",
    )
    (path / "action.json").write_text(
        json.dumps(meta, ensure_ascii=False),
        encoding="utf-8",
    )
    (path / "action.py").write_text(source, encoding="utf-8")


def _prepare_engine_recovery(monkeypatch, tmp_path):
    from notmyfault.host import app
    from notmyfault.security.security import SecurityMode

    paths = make_paths(tmp_path)
    store = make_store(paths)
    monkeypatch.setattr(app, "_ensure_first_run_build", lambda: None)
    monkeypatch.setattr(
        app,
        "_get_plugin_paths",
        lambda _store: [(str(paths.user_plugins_dir), "user")],
    )
    monkeypatch.setattr(
        "notmyfault.core.engine._detect_security_mode",
        lambda: SecurityMode.PERMISSIVE,
    )
    alerts = []
    alert_module = types.ModuleType("notmyfault.host.alert")
    alert_module.alert_user = lambda *args, **kwargs: alerts.append((args, kwargs))
    monkeypatch.setitem(sys.modules, "notmyfault.host.alert", alert_module)
    return app, paths, store, alerts


def test_engine_start_restores_backup_when_updated_plugin_cannot_import(
    monkeypatch,
    tmp_path,
):
    app, paths, store, alerts = _prepare_engine_recovery(monkeypatch, tmp_path)
    action_root = paths.user_plugins_dir / "actions"
    old_backup = action_root / "old_id.nmf-backup"
    new_plugin = action_root / "new_id"
    _write_action(
        old_backup,
        "old_id",
        1,
        "def run(meta, params):\n    return {'version': 1}\n",
    )
    _write_action(
        new_plugin,
        "new_id",
        2,
        "raise RuntimeError('broken update')\n",
    )
    events = []

    engine = app.create_engine(
        store,
        on_event=lambda event_type, data: events.append((event_type, data)),
    )
    try:
        restored = action_root / "old_id"
        assert restored.is_dir()
        assert not old_backup.exists()
        assert not new_plugin.exists()
        restored_meta = json.loads(
            (restored / "action.json").read_text(encoding="utf-8")
        )
        assert restored_meta["version_code"] == 1
        assert engine.actions_funcs["old_id"]({}, {}) == {"version": 1}
        assert events == [
            (
                "plugin_update_rolled_back",
                {
                    "plugins": [
                        {
                            "id": "old_id",
                            "package_name": "com.test.restart_restore",
                            "version_code": 1,
                        }
                    ]
                },
            )
        ]
        assert len(alerts) == 1
    finally:
        engine.shutdown()


def test_engine_start_keeps_loadable_update_and_backup(monkeypatch, tmp_path):
    app, paths, store, alerts = _prepare_engine_recovery(monkeypatch, tmp_path)
    action_root = paths.user_plugins_dir / "actions"
    backup = action_root / "demo.nmf-backup"
    current = action_root / "demo"
    _write_action(
        backup,
        "demo",
        1,
        "def run(meta, params):\n    return {'version': 1}\n",
    )
    _write_action(
        current,
        "demo",
        2,
        "def run(meta, params):\n    return {'version': 2}\n",
    )
    events = []

    engine = app.create_engine(
        store,
        on_event=lambda event_type, data: events.append((event_type, data)),
    )
    try:
        assert current.is_dir()
        assert backup.is_dir()
        assert engine.actions_funcs["demo"]({}, {}) == {"version": 2}
        assert events == []
        assert alerts == []
    finally:
        engine.shutdown()


def test_engine_start_does_not_check_disabled_update(monkeypatch, tmp_path):
    app, paths, store, alerts = _prepare_engine_recovery(monkeypatch, tmp_path)
    config = store.load_config()
    config["disabled_plugins"] = {"triggers": [], "actions": ["demo"]}
    assert store.save_config(config)
    action_root = paths.user_plugins_dir / "actions"
    backup = action_root / "demo.nmf-backup"
    current = action_root / "demo"
    _write_action(
        backup,
        "demo",
        1,
        "def run(meta, params):\n    return {'version': 1}\n",
    )
    _write_action(
        current,
        "demo",
        2,
        "raise RuntimeError('disabled update')\n",
    )
    events = []

    engine = app.create_engine(
        store,
        on_event=lambda event_type, data: events.append((event_type, data)),
    )
    try:
        assert current.is_dir()
        assert backup.is_dir()
        assert "demo" not in engine.actions_funcs
        assert events == []
        assert alerts == []
    finally:
        engine.shutdown()
