from __future__ import annotations

import json

import py7zr
import pytest

from notmyfault.host.api.plugin_installation import PluginFileSystem
from notmyfault.tests.api_support import make_api_env


def make_meta(plugin_kind: str, **overrides):
    meta = {
        "id": f"demo_{plugin_kind}",
        "name": "演示插件",
        "description": "演示用插件",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": f"com.test.demo_{plugin_kind}",
    }
    meta.update(overrides)
    return meta


def build_nmfp(tmp_path, meta, plugin_kind, tag="pkg", extra_files=None):
    json_name = "trigger.json" if plugin_kind == "triggers" else "action.json"
    source = tmp_path / f"source-{tag}" / f"plugin-{tag}"
    source.mkdir(parents=True)
    (source / json_name).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    entry = "trigger.py" if plugin_kind == "triggers" else "action.py"
    (source / entry).write_text(
        "def run(meta, params):\n    return {'ok': True}\n", encoding="utf-8"
    )
    for name, content in (extra_files or {}).items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    archive = tmp_path / f"plugin-{tag}.nmfp"
    with py7zr.SevenZipFile(archive, "w") as output:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                output.write(path, f"plugin-{tag}/{path.relative_to(source).as_posix()}")
    return archive


def post_archive(env, route, archive, data=None):
    with open(archive, "rb") as file:
        return env.client.post(
            route,
            headers=env.headers,
            data=data or {},
            files={"file": (archive.name, file, "application/octet-stream")},
        )


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
        data={"preview_token": preview.json()["preview_token"]},
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
    original = b"def run(meta, params): return 1\n"
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
    (root / "action.py").write_text("def run(meta, params): return 2\n", encoding="utf-8")
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
    assert set(response.json()) == {"triggers", "actions"}


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
