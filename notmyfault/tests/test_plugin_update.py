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
from notmyfault.tests.plugin_support import build_nmfp, make_meta, post_archive


@pytest.fixture(autouse=True)
def normal_plugin_installation_mode(monkeypatch):
    from notmyfault.host.api.services import plugin_installation
    from notmyfault.security.security import SecurityMode

    monkeypatch.setattr(
        plugin_installation, "detect_security_mode", lambda: SecurityMode.NORMAL
    )


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


@pytest.mark.parametrize("tamper", [False, True])
def test_changed_package_id_is_backed_up_and_uninstall_removes_backup(tmp_path, tamper):
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
    other_backup = env.paths.user_plugins_dir / "actions" / "other.nmf-backup"
    other_backup.mkdir()
    (other_backup / "action.json").write_text(
        json.dumps(make_meta("actions", id="other", package_name="other-package")),
        encoding="utf-8",
    )
    if tamper:
        metadata_path = env.paths.user_plugins_dir / "actions" / "new_id" / "action.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["package_name"] = "other-package"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    response = env.client.delete(
        "/api/plugins/actions/new_id", headers=env.headers
    )
    assert response.json()["ok"] is True
    assert backup.exists() is tamper
    assert other_backup.is_dir()


def test_update_after_id_change_keeps_only_latest_backup(tmp_path, monkeypatch):
    from notmyfault.security import plugins as security_plugins

    env = make_api_env(tmp_path)
    first = build_nmfp(tmp_path, make_meta("actions"), "actions", "first-id")
    assert post_archive(env, "/api/plugins/install", first).status_code == 200
    unrelated = build_nmfp(
        tmp_path,
        make_meta("actions", id="unrelated", package_name="com.test.unrelated"),
        "actions",
        "unrelated",
    )
    assert post_archive(env, "/api/plugins/install", unrelated).status_code == 200
    manifest_path = env.paths.plugin_manifest_file
    unrelated_hashes = security_plugins.load_plugin_manifest(manifest_path)["unrelated"]
    save_manifest = security_plugins.save_plugin_manifest
    writes = []

    def record_save(manifest, path):
        writes.append(json.loads(json.dumps(manifest)))
        return save_manifest(manifest, path)

    monkeypatch.setattr(security_plugins, "save_plugin_manifest", record_save)
    renamed_meta = make_meta("actions", id="new_id", version_code=2)
    second = build_nmfp(tmp_path, renamed_meta, "actions", "second-id")
    assert post_archive(env, "/api/plugins/install", second).status_code == 200
    assert len(writes) == 1
    assert set(writes[0]) == {"new_id", "unrelated"}
    assert writes[0]["unrelated"] == unrelated_hashes
    third = build_nmfp(
        tmp_path,
        make_meta("actions", id="new_id", version_code=3),
        "actions",
        "third-id",
    )

    assert post_archive(env, "/api/plugins/install", third).status_code == 200
    assert len(writes) == 2
    assert set(writes[1]) == {"new_id", "unrelated"}
    assert writes[1]["unrelated"] == unrelated_hashes
    assert security_plugins.load_plugin_manifest(manifest_path) == writes[1]

    action_root = env.paths.user_plugins_dir / "actions"
    backups = sorted(action_root.glob("*.nmf-backup"))
    assert [path.name for path in backups] == ["new_id.nmf-backup"]
    backup_meta = json.loads(
        (backups[0] / "action.json").read_text(encoding="utf-8")
    )
    assert backup_meta["version_code"] == 2


@pytest.mark.parametrize("mode", ["strict", "normal", "permissive"])
@pytest.mark.parametrize("operation", ["install", "update", "change_id"])
def test_manifest_save_failure_preserves_plugins_and_backups(
    tmp_path, monkeypatch, mode, operation,
):
    import py7zr
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from notmyfault.host.api.services import plugin_installation
    from notmyfault.security import plugins as security_plugins, signing, signing_keys
    from notmyfault.security.security import SecurityMode

    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ])
    monkeypatch.setattr(
        plugin_installation, "detect_security_mode", lambda: SecurityMode(mode)
    )
    env = make_api_env(tmp_path)

    def signed_archive(meta, tag):
        archive = build_nmfp(tmp_path, meta, "actions", tag)
        root = tmp_path / f"source-{tag}" / f"plugin-{tag}"
        signing.sign_plugin(root, "action.json", private_key=key)
        with py7zr.SevenZipFile(archive, "w") as output:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    output.write(path, f"plugin-{tag}/{path.relative_to(root).as_posix()}")
        return archive

    unrelated = signed_archive(
        make_meta("actions", id="unrelated", package_name="com.test.unrelated"),
        "unrelated",
    )
    assert post_archive(env, "/api/plugins/install", unrelated).status_code == 200
    if operation != "install":
        for version in (1, 2):
            archive = signed_archive(
                make_meta("actions", version_code=version), f"version-{version}"
            )
            assert post_archive(env, "/api/plugins/install", archive).status_code == 200

    action_root = env.paths.user_plugins_dir / "actions"
    manifest_path = env.paths.plugin_manifest_file
    manifest_before = manifest_path.read_bytes()
    files_before = {
        path.relative_to(action_root).as_posix(): path.read_bytes()
        for path in action_root.rglob("*") if path.is_file()
    }
    plugin_id = "new_id" if operation == "change_id" else "demo_actions"
    candidate = signed_archive(
        make_meta("actions", id=plugin_id, version_code=3), "candidate"
    )
    writes = []

    def reject_save(manifest, path):
        writes.append(manifest)
        return False

    monkeypatch.setattr(security_plugins, "save_plugin_manifest", reject_save)
    response = post_archive(env, "/api/plugins/install", candidate)

    assert response.status_code == 500
    assert response.json()["ok"] is False
    assert len(writes) == 1
    assert manifest_path.read_bytes() == manifest_before
    assert {
        path.relative_to(action_root).as_posix(): path.read_bytes()
        for path in action_root.rglob("*") if path.is_file()
    } == files_before
    if operation in ("install", "change_id"):
        assert not (action_root / plugin_id).exists()
    if operation != "install":
        assert (action_root / "demo_actions.nmf-backup").is_dir()


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
    *,
    execution_mode: str = "in-process",
) -> None:
    path.mkdir(parents=True)
    meta = make_meta(
        "actions",
        id=plugin_id,
        package_name="com.test.restart_restore",
        execution_mode=execution_mode,
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
    monkeypatch.setattr(app, "_ensure_first_run_build", lambda package_root: None)
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


@pytest.mark.parametrize("mode,recovery_failure", [
    ("strict", None), ("normal", None), ("permissive", None),
    ("strict", "save"), ("normal", "save"), ("permissive", "save"),
    ("strict", "signature"), ("strict", "import"),
])
@pytest.mark.parametrize("new_id", ["old_id", "new_id"])
@pytest.mark.parametrize("execution_mode", ["in-process", "isolated"])
def test_engine_start_restores_backup_when_updated_plugin_cannot_import(
    monkeypatch,
    tmp_path,
    mode,
    recovery_failure,
    new_id,
    execution_mode,
):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from notmyfault.security import plugins, signing, signing_keys
    from notmyfault.security.plugin_checks import inspect_plugin_tree
    from notmyfault.security.security import SecurityMode

    app, paths, store, alerts = _prepare_engine_recovery(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "notmyfault.core.engine._detect_security_mode", lambda: SecurityMode(mode)
    )
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ])
    action_root = paths.user_plugins_dir / "actions"
    old_backup = action_root / "old_id.nmf-backup"
    new_plugin = action_root / new_id
    _write_action(
        old_backup,
        "old_id",
        1,
        "no_run = True\n" if recovery_failure == "import" else
        "def run(meta, params):\n    return {'version': 1}\n",
        execution_mode=execution_mode,
    )
    _write_action(
        new_plugin,
        new_id,
        2,
        "raise RuntimeError('broken update')\n",
        execution_mode=execution_mode,
    )
    for folder in (old_backup, new_plugin):
        signing.sign_plugin(folder, "action.json", private_key=key)
    old_snapshot = inspect_plugin_tree(str(old_backup)).file_snapshot
    new_snapshot = inspect_plugin_tree(str(new_plugin)).file_snapshot
    unrelated = {"action.py": "a" * 64}
    assert plugins.save_plugin_manifest(
        {new_id: new_snapshot, "unrelated": unrelated}, paths.plugin_manifest_file
    )
    if recovery_failure == "signature":
        (old_backup / "action.py").write_text("no_run = True\n", encoding="utf-8")
    files_before = {
        path.relative_to(action_root).as_posix(): path.read_bytes()
        for path in action_root.rglob("*") if path.is_file()
    }
    manifest_before = paths.plugin_manifest_file.read_bytes()
    if recovery_failure == "save":
        monkeypatch.setattr(plugins, "save_plugin_manifest", lambda *_args: False)
    events = []

    engine = app.create_engine(
        store,
        on_event=lambda event_type, data: events.append((event_type, data)),
    )
    try:
        restored = action_root / "old_id"
        if recovery_failure in ("save", "signature"):
            assert {
                path.relative_to(action_root).as_posix(): path.read_bytes()
                for path in action_root.rglob("*") if path.is_file()
            } == files_before
            assert paths.plugin_manifest_file.read_bytes() == manifest_before
        else:
            assert restored.is_dir()
            assert not old_backup.exists()
            if new_id != "old_id":
                assert not new_plugin.exists()
            assert plugins.load_plugin_manifest(paths.plugin_manifest_file) == {
                "old_id": old_snapshot, "unrelated": unrelated,
            }
        if recovery_failure:
            assert "old_id" not in engine.actions_funcs
            assert [name for name, _data in events if name != "plugin_load_failed"] == [
                "plugin_update_rollback_failed"
            ]
            assert alerts == []
            return
        restored_meta = json.loads(
            (restored / "action.json").read_text(encoding="utf-8")
        )
        assert restored_meta["version_code"] == 1
        assert engine.actions_funcs["old_id"]({}, {}) == {"version": 1}
        # 更新失败会先推一条插件加载失败，回退事件不变
        assert [name for name, _data in events if name != "plugin_load_failed"] == [
            "plugin_update_rolled_back"
        ]
        assert events[-1] == (
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
        assert any(
            name == "plugin_load_failed" and data["plugin_id"] == new_id
            for name, data in events
        )
        assert len(alerts) == 1
    finally:
        engine.shutdown()


@pytest.mark.parametrize("execution_mode", ["in-process", "isolated"])
def test_engine_start_keeps_loadable_update_and_backup(
    monkeypatch, tmp_path, execution_mode,
):
    app, paths, store, alerts = _prepare_engine_recovery(monkeypatch, tmp_path)
    action_root = paths.user_plugins_dir / "actions"
    backup = action_root / "demo.nmf-backup"
    current = action_root / "demo"
    marker = tmp_path / "action-ran.txt"
    _write_action(
        backup,
        "demo",
        1,
        "def run(meta, params):\n    return {'version': 1}\n",
        execution_mode=execution_mode,
    )
    _write_action(
        current,
        "demo",
        2,
        "from pathlib import Path\n"
        "def run(meta, params):\n"
        f"    Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
        "    return {'version': 2}\n",
        execution_mode=execution_mode,
    )
    events = []

    engine = app.create_engine(
        store,
        on_event=lambda event_type, data: events.append((event_type, data)),
    )
    try:
        assert current.is_dir()
        assert backup.is_dir()
        assert not marker.exists()
        assert engine.actions_funcs["demo"]({}, {}) == {"version": 2}
        assert marker.read_text(encoding="utf-8") == "ran"
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
