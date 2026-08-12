"""旧版开关插件写进签名 json 的 enabled 状态迁移到 config 的测试"""

import json

import pytest

import notmyfault.config as config_mod
from notmyfault.host import app as app_mod
from notmyfault.security import plugins as security_plugins


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
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setattr(
        security_plugins, "_PLUGIN_MANIFEST_FILE", str(tmp_path / "manifest.json")
    )
    return tmp_path


def test_migrates_json_disabled_into_config(env):
    json_path = write_user_plugin(
        env, "actions", "demo", make_meta("demo", enabled=False)
    )
    config = {"disabled_plugins": {"triggers": [], "actions": []}}

    migrated = app_mod.migrate_user_plugin_enabled_state(config, user_dir=str(env))

    assert migrated == ["demo"]
    assert config["disabled_plugins"]["actions"] == ["demo"]
    meta = json.loads(json_path.read_text(encoding="utf-8"))
    assert meta["enabled"] is True
    with open(config_mod.CONFIG_FILE, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["disabled_plugins"]["actions"] == ["demo"]


def test_migration_updates_manifest_baseline(env):
    json_path = write_user_plugin(
        env, "actions", "demo", make_meta("demo", enabled=False)
    )
    config = {"disabled_plugins": {"triggers": [], "actions": []}}

    app_mod.migrate_user_plugin_enabled_state(config, user_dir=str(env))

    manifest = security_plugins.load_plugin_manifest()
    assert manifest["demo"]["action.json"] == security_plugins.compute_file_hash(
        str(json_path)
    )


def test_migration_leaves_enabled_plugins_alone(env):
    json_path = write_user_plugin(
        env, "actions", "demo", make_meta("demo", enabled=True)
    )
    original = json_path.read_text(encoding="utf-8")
    config = {"disabled_plugins": {"triggers": [], "actions": []}}

    migrated = app_mod.migrate_user_plugin_enabled_state(config, user_dir=str(env))

    assert migrated == []
    assert config["disabled_plugins"]["actions"] == []
    assert json_path.read_text(encoding="utf-8") == original


def test_migration_is_idempotent(env):
    write_user_plugin(env, "triggers", "demo", make_meta("demo", enabled=False))
    config = {"disabled_plugins": {"triggers": [], "actions": []}}

    first = app_mod.migrate_user_plugin_enabled_state(config, user_dir=str(env))
    second = app_mod.migrate_user_plugin_enabled_state(config, user_dir=str(env))

    assert first == ["demo"]
    assert second == []
    assert config["disabled_plugins"]["triggers"] == ["demo"]


def test_migration_keeps_existing_config_entries(env):
    write_user_plugin(env, "actions", "demo", make_meta("demo", enabled=False))
    config = {"disabled_plugins": {"triggers": [], "actions": ["other"]}}

    app_mod.migrate_user_plugin_enabled_state(config, user_dir=str(env))

    assert config["disabled_plugins"]["actions"] == ["other", "demo"]


def test_migration_missing_user_dir(env):
    config = {"disabled_plugins": {"triggers": [], "actions": []}}
    migrated = app_mod.migrate_user_plugin_enabled_state(
        config, user_dir=str(env / "absent")
    )
    assert migrated == []
