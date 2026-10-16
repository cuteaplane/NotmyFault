from __future__ import annotations

import json
import os

import py7zr
import pytest

from notmyfault.host.api.plugin_installation import PluginFileSystem
from notmyfault.tests.api_support import make_api_env
from notmyfault.tests.plugin_support import build_nmfp, make_meta, post_archive


@pytest.fixture(autouse=True)
def permissive_plugin_installation_mode(monkeypatch):
    from notmyfault.host.api.services import plugin_installation
    from notmyfault.security.security import SecurityMode

    monkeypatch.setattr(
        plugin_installation, "detect_security_mode", lambda: SecurityMode.PERMISSIVE
    )


def test_unsupported_platform_package_is_rejected(tmp_path):
    env = make_api_env(tmp_path)
    other_platform = "linux" if os.name == "nt" else "windows"
    archive = build_nmfp(
        tmp_path, make_meta("actions", platforms=[other_platform]), "actions"
    )

    preview = post_archive(env, "/api/plugins/preview", archive)
    assert preview.status_code == 400
    assert "当前平台" in preview.json()["error"]

    install = post_archive(env, "/api/plugins/install", archive)
    assert install.status_code == 400
    assert not (env.paths.user_plugins_dir / "actions" / "demo_actions").exists()


def test_safe_nmfp_installs_to_injected_user_plugin_path(tmp_path):
    env = make_api_env(tmp_path)
    archive = build_nmfp(tmp_path, make_meta("actions"), "actions")
    response = post_archive(env, "/api/plugins/install", archive)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "id": "demo_actions",
        "type": "actions",
        "package_name": "com.test.demo_actions",
        "version_code": 1,
        "restart_required": True,
        "backup_kept": False,
    }
    installed = env.paths.user_plugins_dir / "actions" / "demo_actions"
    assert (installed / "action.json").is_file()
    assert (installed / "action.py").is_file()


def test_schema_failure_does_not_create_plugin_directory(tmp_path):
    env = make_api_env(tmp_path)
    archive = build_nmfp(tmp_path, {"id": "broken"}, "actions", "broken")
    response = post_archive(env, "/api/plugins/install", archive)
    assert response.status_code == 400
    assert "schema" in response.json()["error"]
    assert not env.paths.user_plugins_dir.exists()


def test_builtin_plugin_id_cannot_be_installed_as_user_plugin(tmp_path):
    env = make_api_env(tmp_path)
    archive = build_nmfp(
        tmp_path,
        make_meta(
            "actions",
            id="notify",
            package_name="com.test.not_the_builtin_notify",
        ),
        "actions",
        "builtin-collision",
    )

    preview = post_archive(env, "/api/plugins/preview", archive)

    assert preview.status_code == 200
    assert "plugin_id_collision" in {
        risk["id"] for risk in preview.json()["risks"]
    }
    installed = env.client.post(
        "/api/plugins/install",
        headers=env.headers,
        data={"preview_token": preview.json()["preview_token"], "confirmed_risk_ids": json.dumps(preview.json()["installation"]["required_risk_ids"])},
    )
    assert installed.status_code == 409
    assert not (env.paths.user_plugins_dir / "actions" / "notify").exists()


def test_builtin_plugin_id_in_other_kind_conflicts(tmp_path):
    env = make_api_env(tmp_path)
    archive = build_nmfp(
        tmp_path,
        make_meta(
            "triggers",
            id="notify",
            package_name="com.test.notify_trigger",
        ),
        "triggers",
        "cross-kind-id",
    )

    installed = post_archive(env, "/api/plugins/install", archive)

    assert installed.status_code == 400
    assert "plugin_id_collision" in {risk["id"] for risk in installed.json()["risks"]}
    assert not (env.paths.user_plugins_dir / "triggers" / "notify").exists()


@pytest.mark.parametrize("value", [[], "manifest", None])
def test_non_object_manifest_is_rejected_by_preview_and_reported_in_catalog(tmp_path, value):
    env = make_api_env(tmp_path)
    archive = build_nmfp(tmp_path, value, "actions")
    preview = post_archive(env, "/api/plugins/preview", archive)
    assert preview.status_code == 400
    plugin = env.paths.user_plugins_dir / "actions" / "invalid"
    plugin.mkdir(parents=True)
    (plugin / "action.json").write_text(json.dumps(value), encoding="utf-8")
    listed = env.client.get("/api/plugins/list", headers=env.headers)
    assert listed.status_code == 200
    assert "JSON 对象" in listed.json()["actions"]["invalid"]["_error"]


def test_strict_install_validates_signature_before_replacing_installed_plugin(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from notmyfault.host.api.services import plugin_installation
    from notmyfault.security import signing, signing_keys
    from notmyfault.security.security import SecurityMode

    env = make_api_env(tmp_path)
    meta = make_meta("actions", author="插件作者", build={"outputs": ["action.py"]})
    root = tmp_path / "signed"
    root.mkdir()
    (root / "action.json").write_text(json.dumps(meta), encoding="utf-8")
    original = b"def run_with_context(meta, params, context): return 1\n"
    (root / "action.py").write_bytes(original)
    author = Ed25519PrivateKey.generate()
    owner = Ed25519PrivateKey.generate()
    signing.self_sign_plugin(root, author)
    monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [
        owner.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ])
    monkeypatch.setattr(plugin_installation, "detect_security_mode", lambda: SecurityMode.STRICT)
    monkeypatch.setattr(plugin_installation.PluginInstallationService, "_counter_sign_author_key",
                        lambda self, path, password: signing.counter_sign_author_key(path, owner) and None)

    def install(tag):
        archive = tmp_path / f"{tag}.nmfp"
        with py7zr.SevenZipFile(archive, "w") as output:
            for path in root.iterdir():
                output.write(path, f"signed/{path.name}")
        preview = post_archive(env, "/api/plugins/preview", archive)
        assert preview.status_code == 200
        return env.client.post("/api/plugins/install", headers=env.headers, data={
            "preview_token": preview.json()["preview_token"],
            "confirmed_risk_ids": '["build_hook"]',
        })

    assert install("valid").status_code == 200
    installed = env.paths.user_plugins_dir / "actions" / meta["id"]
    assert signing.verify_author_key_counter_signature(installed, signing_keys.get_public_keys())
    assert (installed / "signature.sig").read_bytes() == (root / "signature.sig").read_bytes()
    (root / "action.py").write_text("def run_with_context(meta, params, context): return 2\n", encoding="utf-8")
    rejected = install("changed")
    assert rejected.status_code == 400
    assert "签名无效" in rejected.json()["error"]
    assert (installed / "action.py").read_bytes() == original


def test_preview_token_installs_the_exact_previewed_directory(tmp_path):
    env = make_api_env(tmp_path)
    meta = make_meta("actions")
    archive = build_nmfp(tmp_path, meta, "actions", "preview")
    preview = post_archive(env, "/api/plugins/preview", archive)
    assert preview.status_code == 200
    token = preview.json()["preview_token"]
    installed = env.client.post(
        "/api/plugins/install",
        headers=env.headers,
        data={"preview_token": token},
    )
    assert installed.status_code == 200
    reused = env.client.post(
        "/api/plugins/install",
        headers=env.headers,
        data={"preview_token": token},
    )
    assert reused.status_code == 400


def test_build_hook_requires_matching_server_confirmation(tmp_path):
    env = make_api_env(tmp_path)
    archive = build_nmfp(
        tmp_path,
        make_meta("actions", build={"outputs": ["action.py"]}),
        "actions",
        "build-confirmation",
    )

    def preview_token():
        response = post_archive(env, "/api/plugins/preview", archive)
        assert response.status_code == 200
        return response.json()["preview_token"]

    missing = env.client.post(
        "/api/plugins/install",
        headers=env.headers,
        data={"preview_token": preview_token()},
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "build_hook_confirmation_required"

    wrong = env.client.post(
        "/api/plugins/install",
        headers=env.headers,
        data={
            "preview_token": preview_token(),
            "confirmed_risk_ids": '["borrowed_privilege"]',
        },
    )
    assert wrong.status_code == 400
    assert "不一致" in wrong.json()["error"]

    confirmed = env.client.post(
        "/api/plugins/install",
        headers=env.headers,
        data={
            "preview_token": preview_token(),
            "confirmed_risk_ids": '["build_hook"]',
        },
    )
    assert confirmed.status_code == 200


def test_unsigned_build_command_is_not_executed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from notmyfault.host.api.services import plugin_installation

    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    env = make_api_env(tmp_path)
    monkeypatch.setattr(plugin_installation.subprocess, "run", fake_run)
    archive = build_nmfp(
        tmp_path,
        make_meta(
            "actions",
            build={"command": ["echo pwned"], "outputs": ["action.py"]},
        ),
        "actions",
        "unsigned-build",
    )
    preview = post_archive(env, "/api/plugins/preview", archive)
    assert preview.status_code == 200
    installed = env.client.post(
        "/api/plugins/install",
        headers=env.headers,
        data={
            "preview_token": preview.json()["preview_token"],
            "confirmed_risk_ids": '["build_hook"]',
        },
    )
    assert installed.status_code == 400
    assert "签名" in installed.json()["error"]
    assert calls == []


def test_build_command_runs_as_argv_without_shell(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from notmyfault.host.api.services import plugin_installation
    from notmyfault.host.api.services.plugin_installation import (
        PluginInstallationService,
    )

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["shell"] = kwargs.get("shell")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin_installation.subprocess, "run", fake_run)
    monkeypatch.setattr(
        plugin_installation, "plugin_signature_kind", lambda *a, **k: "author"
    )
    PluginInstallationService._run_build_hook(
        tmp_path,
        {"build": {"command": ["python setup.py"]}},
    )
    assert captured["shell"] is False
    assert captured["command"] == ["python", "setup.py"]


def test_downgrade_requires_force_and_keeps_backup(tmp_path):
    env = make_api_env(tmp_path)
    high = build_nmfp(
        tmp_path,
        make_meta("actions", version_code=2, version="2.0"),
        "actions",
        "high",
    )
    assert post_archive(env, "/api/plugins/install", high).status_code == 200
    low = build_nmfp(tmp_path, make_meta("actions"), "actions", "low")
    rejected = post_archive(env, "/api/plugins/install", low)
    assert rejected.status_code == 400
    forced = post_archive(
        env,
        "/api/plugins/install",
        low,
        data={"force": "true"},
    )
    assert forced.status_code == 200
    assert forced.json()["backup_kept"] is True
    assert (
        env.paths.user_plugins_dir / "actions" / "demo_actions.nmf-backup"
    ).is_dir()


def test_same_package_can_change_id_in_one_transaction(tmp_path):
    env = make_api_env(tmp_path)
    first = build_nmfp(tmp_path, make_meta("actions"), "actions", "first")
    assert post_archive(env, "/api/plugins/install", first).status_code == 200
    renamed = build_nmfp(
        tmp_path,
        make_meta("actions", id="renamed", version_code=2),
        "actions",
        "renamed",
    )
    response = post_archive(env, "/api/plugins/install", renamed)
    assert response.status_code == 200
    root = env.paths.user_plugins_dir / "actions"
    assert (root / "renamed").is_dir()
    assert not (root / "demo_actions").exists()
    assert (root / "demo_actions.nmf-backup").is_dir()


def test_toggle_and_uninstall_use_signed_config_store(tmp_path):
    env = make_api_env(tmp_path)
    archive = build_nmfp(tmp_path, make_meta("actions"), "actions")
    assert post_archive(env, "/api/plugins/install", archive).status_code == 200
    toggled = env.client.post(
        "/api/plugins/toggle",
        json={"type": "actions", "id": "demo_actions"},
        headers=env.headers,
    )
    assert toggled.json()["enabled"] is False
    config = env.store.load_verified_config()
    assert config["disabled_plugins"]["actions"] == ["demo_actions"]
    removed = env.client.delete(
        "/api/plugins/actions/demo_actions", headers=env.headers
    )
    assert removed.json() == {"ok": True, "restart_required": True}


def test_plugin_list_enabled_follows_disabled_plugins_config(tmp_path):
    env = make_api_env(tmp_path)
    listed = env.client.get("/api/plugins/list", headers=env.headers).json()
    assert listed["actions"]["notify"]["enabled"] is True
    assert listed["triggers"]["time_schedule"]["enabled"] is True

    config = env.store.load_verified_config()
    config["disabled_plugins"] = {"triggers": ["time_schedule"], "actions": ["notify"]}
    assert env.store.save_config(config)

    listed = env.client.get("/api/plugins/list", headers=env.headers).json()
    assert listed["actions"]["notify"]["enabled"] is False
    assert listed["triggers"]["time_schedule"]["enabled"] is False

    config = env.store.load_verified_config()
    config["disabled_plugins"] = {"triggers": [], "actions": []}
    assert env.store.save_config(config)

    listed = env.client.get("/api/plugins/list", headers=env.headers).json()
    assert listed["actions"]["notify"]["enabled"] is True
    assert listed["triggers"]["time_schedule"]["enabled"] is True


def test_plugin_list_enabled_follows_config_for_user_plugin(tmp_path):
    env = make_api_env(tmp_path)
    archive = build_nmfp(tmp_path, make_meta("actions"), "actions")
    assert post_archive(env, "/api/plugins/install", archive).status_code == 200
    listed = env.client.get("/api/plugins/list", headers=env.headers).json()
    assert listed["actions"]["demo_actions"]["enabled"] is True

    config = env.store.load_verified_config()
    config["disabled_plugins"] = {"triggers": [], "actions": ["demo_actions"]}
    assert env.store.save_config(config)

    listed = env.client.get("/api/plugins/list", headers=env.headers).json()
    assert listed["actions"]["demo_actions"]["enabled"] is False


def test_plugin_list_locks_switches_when_config_cannot_be_verified(tmp_path):
    env = make_api_env(tmp_path)
    raw = json.loads(env.paths.config_file.read_text(encoding="utf-8"))
    raw["disabled_plugins"] = {"triggers": [], "actions": []}
    env.paths.config_file.write_text(json.dumps(raw), encoding="utf-8")

    payload = env.client.get("/api/plugins/list", headers=env.headers).json()

    assert payload["config_error"]
    assert payload["actions"]["notify"]["enabled"] is False
    assert payload["actions"]["notify"]["_error"] == payload["config_error"]


def test_uninstall_reports_strict_delete_failure(tmp_path):
    class FailingRemovalFileSystem(PluginFileSystem):
        def remove_tree(self, path):
            raise PermissionError("plugin directory is locked")

    env = make_api_env(
        tmp_path,
        plugin_file_system=FailingRemovalFileSystem(),
    )
    plugin_dir = env.paths.user_plugins_dir / "actions" / "locked_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "action.json").write_text(
        json.dumps(
            make_meta(
                "actions",
                id="locked_plugin",
                package_name="com.test.locked_plugin",
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = env.client.delete(
        "/api/plugins/actions/locked_plugin",
        headers=env.headers,
    )

    assert response.json() == {"ok": False, "error": "删除插件文件失败"}
    assert plugin_dir.is_dir()


def test_plugin_list_contract_has_both_kinds(tmp_path):
    env = make_api_env(tmp_path)
    response = env.client.get("/api/plugins/list", headers=env.headers)
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"triggers", "actions", "runtime"}
    runtime = payload["runtime"]
    assert set(runtime) == {
        "engine_state",
        "diagnostics_available",
        "plugin_errors",
        "plugin_error_count",
    }
    # 没有引擎对象时不能假装加载成功，也不能显示成零错误
    assert runtime["diagnostics_available"] is False
    assert runtime["plugin_errors"] == []
    assert runtime["engine_state"] == "stopped"


class _DiagnosticsEngine:
    """只用于目录接口的会话诊断：返回给定错误或直接抛异常"""

    def __init__(self, errors=None, raises=None):
        self._errors = errors or []
        self._raises = raises

    def get_diagnostics(self):
        if self._raises is not None:
            raise self._raises
        return {"plugins": {"errors": self._errors, "error_count": len(self._errors)}}


def test_plugin_list_separates_directory_from_session_errors(tmp_path):
    env = make_api_env(tmp_path)
    env.runner.engine_state = "running"
    env.runner.current_engine = _DiagnosticsEngine([
        ("Action", "notify", "插件加载失败: RuntimeError"),
        ("Action", "ghost_plugin", "签名无效，拒绝加载"),
        ("plugin", "weird_plugin", "扩展命令入口缺失"),
    ])
    payload = env.client.get("/api/plugins/list", headers=env.headers).json()

    # 目录里有的插件挂上失败原因
    assert payload["actions"]["notify"]["_error"] == "插件加载失败: RuntimeError"
    # 目录里没有的记录不合成插件条目
    assert "ghost_plugin" not in payload["actions"]
    assert "weird_plugin" not in payload["actions"]

    runtime = payload["runtime"]
    assert runtime["engine_state"] == "running"
    assert runtime["diagnostics_available"] is True
    assert runtime["plugin_error_count"] == 3
    assert [item["plugin_id"] for item in runtime["plugin_errors"]] == [
        "notify", "ghost_plugin", "weird_plugin",
    ]
    assert runtime["plugin_errors"][2]["store"] == "plugin"


def test_plugin_list_does_not_claim_errors_for_shadowed_ids(tmp_path):
    env = make_api_env(tmp_path)
    # 用户插件的目录名和声明的 id 不同，诊断里的 notify 是哪一份无从确认
    shadow = env.paths.user_plugins_dir / "actions" / "user_copy_of_notify"
    shadow.mkdir(parents=True)
    (shadow / "action.json").write_text(
        json.dumps({
            "id": "notify", "name": "用户版通知",
            "platforms": ["windows", "linux"], "permissions": [],
            "params": [], "outputs": [],
        }),
        encoding="utf-8",
    )
    env.runner.engine_state = "running"
    env.runner.current_engine = _DiagnosticsEngine(
        [("Action", "notify", "插件加载失败: RuntimeError")]
    )

    payload = env.client.get("/api/plugins/list", headers=env.headers).json()

    assert "_error" not in payload["actions"]["notify"]
    assert payload["runtime"]["plugin_errors"][0]["plugin_id"] == "notify"


def test_plugin_list_reports_diagnostics_read_failure(tmp_path):
    env = make_api_env(tmp_path)
    env.runner.current_engine = _DiagnosticsEngine(raises=RuntimeError("boom"))
    runtime = env.client.get("/api/plugins/list", headers=env.headers).json()["runtime"]

    assert runtime["diagnostics_available"] is False
    assert runtime["diagnostics_error"] == "RuntimeError"
    assert runtime["plugin_errors"] == []


def test_plugin_list_shows_unreadable_manifest_as_broken(tmp_path):
    env = make_api_env(tmp_path)
    plugin = env.paths.user_plugins_dir / "actions" / "broken_json"
    plugin.mkdir(parents=True)
    (plugin / "action.json").write_text("{ 不是 JSON", encoding="utf-8")

    listed = env.client.get("/api/plugins/list", headers=env.headers)
    assert listed.status_code == 200
    entry = listed.json()["actions"]["broken_json"]
    # 身份无法确认：只按目录名展示错误，界面不给启禁和卸载
    assert entry["_unidentified"] is True
    assert entry["origin"] == "user"
    assert "JSON 解析失败" in entry["_error"]


def test_plugin_list_probes_capabilities_once(monkeypatch, tmp_path):
    calls = []

    def probe():
        calls.append(True)
        return {}

    monkeypatch.setattr(
        "notmyfault.host.api.services.plugin_catalog.probe_capabilities",
        probe,
    )
    env = make_api_env(tmp_path)
    response = env.client.get("/api/plugins/list", headers=env.headers)

    assert response.status_code == 200
    assert calls == [True]
