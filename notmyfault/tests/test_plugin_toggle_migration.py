"""旧版开关插件写进签名 json 的 enabled 状态迁移到 config 的测试"""

import json
from types import SimpleNamespace

import pytest

from notmyfault.host import app as app_mod
from notmyfault.security import plugins as security_plugins
from notmyfault.tests.api_support import make_paths, make_store


def make_meta(plugin_id, enabled):
    return {
        "id": plugin_id,
        "name": "测试插件",
        "description": "测试用插件",
        "enabled": enabled,
        "version_code": 1,
        "version": "1.0",
        "package_name": f"com.test.{plugin_id}",
    }


def write_user_plugin(user_dir, ptype, folder, meta):
    json_name = "trigger.json" if ptype == "triggers" else "action.json"
    plugin_dir = user_dir / ptype / folder
    plugin_dir.mkdir(parents=True)
    json_path = plugin_dir / json_name
    json_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return json_path


@pytest.fixture
def env(tmp_path):
    paths = make_paths(tmp_path)
    return SimpleNamespace(
        paths=paths,
        store=make_store(paths),
        user_dir=paths.user_plugins_dir,
    )


def test_migrates_json_disabled_into_config(env):
    json_path = write_user_plugin(
        env.user_dir, "actions", "demo", make_meta("demo", enabled=False)
    )
    config = {"disabled_plugins": {"triggers": [], "actions": []}}

    migrated = app_mod.migrate_user_plugin_enabled_state(
        config,
        env.store,
        user_dir=str(env.user_dir),
    )

    assert migrated == ["demo"]
    assert config["disabled_plugins"]["actions"] == ["demo"]
    meta = json.loads(json_path.read_text(encoding="utf-8"))
    assert meta["enabled"] is True
    saved = json.loads(env.paths.config_file.read_text(encoding="utf-8"))
    assert saved["disabled_plugins"]["actions"] == ["demo"]


def test_migration_updates_manifest_baseline(env):
    json_path = write_user_plugin(
        env.user_dir, "actions", "demo", make_meta("demo", enabled=False)
    )
    config = {"disabled_plugins": {"triggers": [], "actions": []}}

    app_mod.migrate_user_plugin_enabled_state(
        config,
        env.store,
        user_dir=str(env.user_dir),
    )

    manifest = security_plugins.load_plugin_manifest(env.paths.plugin_manifest_file)
    assert manifest["demo"]["action.json"] == security_plugins.compute_file_hash(
        str(json_path)
    )


def test_migration_leaves_enabled_plugins_alone(env):
    json_path = write_user_plugin(
        env.user_dir, "actions", "demo", make_meta("demo", enabled=True)
    )
    original = json_path.read_text(encoding="utf-8")
    config = {"disabled_plugins": {"triggers": [], "actions": []}}

    migrated = app_mod.migrate_user_plugin_enabled_state(
        config,
        env.store,
        user_dir=str(env.user_dir),
    )

    assert migrated == []
    assert config["disabled_plugins"]["actions"] == []
    assert json_path.read_text(encoding="utf-8") == original


def test_migration_is_idempotent(env):
    write_user_plugin(
        env.user_dir,
        "triggers",
        "demo",
        make_meta("demo", enabled=False),
    )
    config = {"disabled_plugins": {"triggers": [], "actions": []}}

    first = app_mod.migrate_user_plugin_enabled_state(
        config,
        env.store,
        user_dir=str(env.user_dir),
    )
    second = app_mod.migrate_user_plugin_enabled_state(
        config,
        env.store,
        user_dir=str(env.user_dir),
    )

    assert first == ["demo"]
    assert second == []
    assert config["disabled_plugins"]["triggers"] == ["demo"]


def test_migration_keeps_existing_config_entries(env):
    write_user_plugin(
        env.user_dir,
        "actions",
        "demo",
        make_meta("demo", enabled=False),
    )
    config = {"disabled_plugins": {"triggers": [], "actions": ["other"]}}

    app_mod.migrate_user_plugin_enabled_state(
        config,
        env.store,
        user_dir=str(env.user_dir),
    )

    assert config["disabled_plugins"]["actions"] == ["other", "demo"]


def test_migration_missing_user_dir(env):
    config = {"disabled_plugins": {"triggers": [], "actions": []}}
    migrated = app_mod.migrate_user_plugin_enabled_state(
        config,
        env.store,
        user_dir=str(env.user_dir / "absent"),
    )
    assert migrated == []
