"""插件管理 API：nmfp 安装/预览、密钥状态、插件列表与卸载"""

import json
from types import SimpleNamespace

import py7zr
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import notmyfault.config as config_mod
from notmyfault.host import api_server
from notmyfault.host.api_server import EngineAPI


class FakeRunner:
    engine_running = False
    engine_state = "stopped"
    current_engine = None

    def start_engine(self):
        return True

    def stop_engine(self):
        return True

    def request_process_shutdown(self, force_after=10):
        pass


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr(api_server, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(api_server, "API_TOKEN_FILE", str(tmp_path / ".api_token"))
    monkeypatch.setattr(api_server, "_PRIVATE_DIR", tmp_path / "private")
    api = EngineAPI(FakeRunner())
    user_dir = tmp_path / "user_plugins"
    api._get_user_plugins_dir = lambda: str(user_dir)
    client = TestClient(api.app)
    headers = {"Authorization": f"Bearer {api_server.API_TOKEN}"}
    return SimpleNamespace(
        api=api, client=client, headers=headers, tmp_path=tmp_path, user_dir=user_dir
    )


def make_meta(ptype, **overrides):
    meta = {
        "id": f"demo_{ptype}",
        "name": "演示插件",
        "description": "演示用插件",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": f"com.test.demo_{ptype}",
    }
    meta.update(overrides)
    return meta


def build_nmfp(tmp_path, meta, ptype, tag="pkg", sign=False):
    """把插件目录打包成 nmfp，归档内恰好一个插件文件夹"""
    json_name = "trigger.json" if ptype == "triggers" else "action.json"
    source = tmp_path / f"src_{tag}" / f"plugin_{tag}"
    source.mkdir(parents=True)
    (source / json_name).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (source / "main.py").write_text(
        "def run(event, params):\n    return True\n", encoding="utf-8"
    )
    if sign:
        from notmyfault.security.signing import self_sign_plugin
        self_sign_plugin(source, Ed25519PrivateKey.generate())
    archive = tmp_path / f"plugin_{tag}.nmfp"
    folder = f"plugin_{tag}"
    with py7zr.SevenZipFile(str(archive), "w") as zf:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                zf.write(path, f"{folder}/{path.relative_to(source).as_posix()}")
    return archive


def install_file(client, headers, archive):
    with open(archive, "rb") as f:
        return client.post(
            "/api/plugins/install",
            headers=headers,
            files={"file": (archive.name, f, "application/octet-stream")},
        )


def upload_preview(client, headers, archive):
    with open(archive, "rb") as f:
        return client.post(
            "/api/plugins/preview",
            headers=headers,
            files={"file": (archive.name, f, "application/octet-stream")},
        )


def write_test_key(private_dir, encrypted=False):
    private_dir.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    encryption = (
        serialization.BestAvailableEncryption(b"secret")
        if encrypted
        else serialization.NoEncryption()
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        encryption,
    )
    (private_dir / "signing_private_key.pem").write_bytes(pem)


def place_bundled_bluetooth(api_env, monkeypatch, *, sign=True):
    from notmyfault.security import signing, signing_keys

    package_root = api_env.tmp_path / "package"
    plugin_dir = package_root / "bundled" / "actions" / "bluetooth_toggle"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "action.json").write_text(
        json.dumps(
            make_meta(
                "actions",
                id="bluetooth_toggle",
                name="蓝牙开关",
                package_name="io.github.notmyfault.bluetooth_toggle",
                permissions=["admin", "external_binary"],
                platforms=["windows", "linux"],
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plugin_dir / "action.py").write_text(
        "def run(meta, params):\n    return {'ok': True}\n", encoding="utf-8"
    )
    monkeypatch.setattr(api_server, "_PKG_ROOT", package_root)
    if sign:
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [public_key])
        signing.sign_plugin(plugin_dir, "action.json", private_key=private_key)
    return plugin_dir


class TestPluginInstall:
    def test_install_trigger(self, api_env):
        write_test_key(api_env.tmp_path / "private")
        meta = make_meta("triggers")
        archive = build_nmfp(api_env.tmp_path, meta, "triggers")
        response = install_file(api_env.client, api_env.headers, archive)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["type"] == "triggers"
        assert body["id"] == meta["id"]

    def test_install_action(self, api_env):
        write_test_key(api_env.tmp_path / "private")
        meta = make_meta("actions")
        archive = build_nmfp(api_env.tmp_path, meta, "actions")
        response = install_file(api_env.client, api_env.headers, archive)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["type"] == "actions"

    def test_install_trigger_success(self, api_env):
        write_test_key(api_env.tmp_path / "private")
        meta = make_meta("triggers")
        archive = build_nmfp(api_env.tmp_path, meta, "triggers")
        assert install_file(api_env.client, api_env.headers, archive).json()["ok"]
        dest = api_env.user_dir / "triggers" / meta["id"]
        assert (dest / "trigger.json").exists()
        # 引擎不再代签，归档没带签名的插件按未签名安装
        assert not (dest / "signature.sig").exists()

    def test_install_keeps_author_signature(self, api_env):
        write_test_key(api_env.tmp_path / "private")
        meta = make_meta("triggers")
        archive = build_nmfp(
            api_env.tmp_path, meta, "triggers", tag="selfsigned", sign=True
        )
        response = install_file(api_env.client, api_env.headers, archive)
        assert response.json()["ok"], response.text
        dest = api_env.user_dir / "triggers" / meta["id"]
        # 作者随包携带的签名和公钥原样保留，引擎不重签
        assert (dest / "signature.sig").exists()
        assert (dest / "public_key.pem").exists()

    def test_install_action_success(self, api_env):
        write_test_key(api_env.tmp_path / "private")
        meta = make_meta("actions")
        archive = build_nmfp(api_env.tmp_path, meta, "actions")
        assert install_file(api_env.client, api_env.headers, archive).json()["ok"]
        dest = api_env.user_dir / "actions" / meta["id"]
        assert (dest / "action.json").exists()
        assert (dest / "main.py").exists()

    def test_no_auth_rejected(self, api_env):
        archive = build_nmfp(api_env.tmp_path, make_meta("triggers"), "triggers")
        response = install_file(api_env.client, {}, archive)
        assert response.status_code == 403

    def test_no_file_rejected(self, api_env):
        response = api_env.client.post(
            "/api/plugins/install", headers=api_env.headers
        )
        assert response.status_code == 400
        assert response.json()["ok"] is False

    def test_schema_invalid(self, api_env):
        # 预览端点报告 schema 问题而不是直接拒绝
        meta = make_meta("triggers")
        del meta["name"]
        archive = build_nmfp(api_env.tmp_path, meta, "triggers", tag="bad")
        response = upload_preview(api_env.client, api_env.headers, archive)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["schema_valid"] is False
        assert body["schema_errors"]

    def test_schema_invalid_rejected(self, api_env):
        write_test_key(api_env.tmp_path / "private")
        meta = make_meta("triggers")
        del meta["name"]
        archive = build_nmfp(api_env.tmp_path, meta, "triggers", tag="bad")
        response = install_file(api_env.client, api_env.headers, archive)
        assert response.status_code == 400
        assert "schema 校验失败" in response.json()["error"]

    def test_duplicate_install_rejected(self, api_env):
        write_test_key(api_env.tmp_path / "private")
        new = build_nmfp(
            api_env.tmp_path, make_meta("triggers", version_code=2), "triggers", "v2"
        )
        assert install_file(api_env.client, api_env.headers, new).json()["ok"]
        old = build_nmfp(
            api_env.tmp_path, make_meta("triggers", version_code=1), "triggers", "v1"
        )
        response = install_file(api_env.client, api_env.headers, old)
        assert response.status_code == 400
        assert "已安装更高版本" in response.json()["error"]

    def test_duplicate_rejected(self, api_env):
        # preview_token 安装后即失效，不能重复消费
        write_test_key(api_env.tmp_path / "private")
        archive = build_nmfp(
            api_env.tmp_path, make_meta("actions"), "actions", "tok"
        )
        preview = upload_preview(api_env.client, api_env.headers, archive).json()
        assert preview["ok"] is True
        token = preview["preview_token"]
        first = api_env.client.post(
            "/api/plugins/install",
            headers=api_env.headers,
            data={"preview_token": token},
        )
        assert first.json()["ok"] is True
        second = api_env.client.post(
            "/api/plugins/install",
            headers=api_env.headers,
            data={"preview_token": token},
        )
        assert second.status_code == 400
        assert second.json()["ok"] is False

    def test_same_version_reinstalls(self, api_env):
        write_test_key(api_env.tmp_path / "private")
        first = build_nmfp(
            api_env.tmp_path, make_meta("triggers"), "triggers", "same1"
        )
        assert install_file(api_env.client, api_env.headers, first).json()["ok"]
        second = build_nmfp(
            api_env.tmp_path, make_meta("triggers"), "triggers", "same2"
        )
        response = install_file(api_env.client, api_env.headers, second)
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True


class TestPluginKeyStatus:
    def test_no_key_file(self, api_env):
        (api_env.tmp_path / "private").mkdir(parents=True, exist_ok=True)
        response = api_env.client.get(
            "/api/plugins/key-status", headers=api_env.headers
        )
        assert response.json() == {"exists": False, "encrypted": False}

    def test_key_not_found(self, api_env):
        # 密钥目录本身不存在时同样报告缺失
        response = api_env.client.get(
            "/api/plugins/key-status", headers=api_env.headers
        )
        assert response.status_code == 200
        assert response.json()["exists"] is False

    def test_key_exists(self, api_env):
        write_test_key(api_env.tmp_path / "private", encrypted=True)
        response = api_env.client.get(
            "/api/plugins/key-status", headers=api_env.headers
        )
        assert response.json() == {"exists": True, "encrypted": True}

    def test_key_exists_unencrypted(self, api_env):
        write_test_key(api_env.tmp_path / "private", encrypted=False)
        response = api_env.client.get(
            "/api/plugins/key-status", headers=api_env.headers
        )
        assert response.json() == {"exists": True, "encrypted": False}


class TestPluginList:
    def test_returns_trigger_and_action_keys(self, api_env):
        response = api_env.client.get("/api/plugins/list", headers=api_env.headers)
        assert response.status_code == 200
        assert set(response.json()) == {"triggers", "actions"}

    def test_lists_builtin_plugins(self, api_env):
        body = api_env.client.get(
            "/api/plugins/list", headers=api_env.headers
        ).json()
        assert "usb_insert" in body["triggers"]
        assert body["triggers"]["usb_insert"]["origin"] == "builtin"
        assert "open_url" in body["actions"]


class TestBundledBluetooth:
    def test_permissive_mode_accepts_unsigned_bundle(self, api_env, monkeypatch):
        from notmyfault.security.security import SecurityMode

        place_bundled_bluetooth(api_env, monkeypatch, sign=False)
        monkeypatch.setattr(
            api_server, "detect_security_mode", lambda: SecurityMode.PERMISSIVE
        )

        response = api_env.client.get(
            "/api/settings/bluetooth", headers=api_env.headers
        )

        assert response.status_code == 200
        assert response.json()["available"] is True

    def test_status_reports_signed_bundle_not_installed(self, api_env, monkeypatch):
        place_bundled_bluetooth(api_env, monkeypatch)

        response = api_env.client.get(
            "/api/settings/bluetooth", headers=api_env.headers
        )

        assert response.status_code == 200
        assert response.json()["available"] is True
        assert response.json()["installed"] is False
        assert response.json()["meta"]["id"] == "bluetooth_toggle"

    def test_install_copies_signed_bundle_as_user_plugin(self, api_env, monkeypatch):
        place_bundled_bluetooth(api_env, monkeypatch)

        response = api_env.client.post(
            "/api/settings/bluetooth/install", headers=api_env.headers
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True, "restart_required": True}
        destination = api_env.user_dir / "actions" / "bluetooth_toggle"
        assert (destination / "action.json").exists()
        listed = api_env.client.get(
            "/api/plugins/list", headers=api_env.headers
        ).json()
        assert listed["actions"]["bluetooth_toggle"]["origin"] == "user"

    def test_install_rejects_tampered_bundle(self, api_env, monkeypatch):
        plugin_dir = place_bundled_bluetooth(api_env, monkeypatch)
        (plugin_dir / "action.py").write_text(
            "def run(meta, params):\n    return {'tampered': True}\n", encoding="utf-8"
        )

        response = api_env.client.post(
            "/api/settings/bluetooth/install", headers=api_env.headers
        )

        assert response.status_code == 400
        assert response.json()["ok"] is False
        assert not (api_env.user_dir / "actions" / "bluetooth_toggle").exists()

    def test_uninstall_removes_installed_bluetooth_action(self, api_env, monkeypatch):
        place_bundled_bluetooth(api_env, monkeypatch)
        api_env.client.post(
            "/api/settings/bluetooth/install", headers=api_env.headers
        )

        response = api_env.client.post(
            "/api/settings/bluetooth/uninstall", headers=api_env.headers
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True, "restart_required": True}
        assert not (api_env.user_dir / "actions" / "bluetooth_toggle").exists()


class TestPluginToggle:
    def place_user_plugin(self, api_env, ptype="actions", pid="myplug", enabled=True):
        json_name = "action.json" if ptype == "actions" else "trigger.json"
        plugin_dir = api_env.user_dir / ptype / pid
        plugin_dir.mkdir(parents=True)
        content = json.dumps(
            make_meta(ptype, id=pid, enabled=enabled), ensure_ascii=False
        )
        (plugin_dir / json_name).write_text(content, encoding="utf-8")
        return plugin_dir, json_name, content

    def toggle(self, api_env, ptype, pid):
        return api_env.client.post(
            "/api/plugins/toggle",
            headers=api_env.headers,
            json={"type": ptype, "id": pid},
        )

    def read_saved_config(self, api_env):
        with open(config_mod.CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_toggle_user_plugin_keeps_json_untouched(self, api_env):
        plugin_dir, json_name, original = self.place_user_plugin(api_env)
        body = self.toggle(api_env, "actions", "myplug").json()
        assert body["ok"] is True
        assert body["enabled"] is False
        assert body["origin"] == "user"
        # 带签名的 json 一个字节都不该动，禁用状态进 config
        assert (plugin_dir / json_name).read_text(encoding="utf-8") == original
        saved = self.read_saved_config(api_env)
        assert saved["disabled_plugins"]["actions"] == ["myplug"]

    def test_toggle_rejects_tampered_config(self, api_env):
        self.place_user_plugin(api_env)
        assert api_env.api._save_config({"custom_flag": "keep"}) is True
        tampered = self.read_saved_config(api_env)
        tampered["custom_flag"] = "changed outside NotmyFault"
        with open(config_mod.CONFIG_FILE, "w", encoding="utf-8") as file:
            json.dump(tampered, file, ensure_ascii=False)

        response = self.toggle(api_env, "actions", "myplug")

        assert response.json()["ok"] is False
        assert "完整性校验" in response.json()["error"]
        assert self.read_saved_config(api_env)["custom_flag"] == "changed outside NotmyFault"

    def test_toggle_user_plugin_twice_restores_enabled(self, api_env):
        plugin_dir, json_name, original = self.place_user_plugin(api_env)
        assert self.toggle(api_env, "actions", "myplug").json()["enabled"] is False
        body = self.toggle(api_env, "actions", "myplug").json()
        assert body["ok"] is True
        assert body["enabled"] is True
        assert (plugin_dir / json_name).read_text(encoding="utf-8") == original
        saved = self.read_saved_config(api_env)
        assert saved["disabled_plugins"]["actions"] == []

    def test_toggle_builtin_plugin_writes_config(self, api_env):
        body = self.toggle(api_env, "actions", "open_url").json()
        assert body["ok"] is True
        assert body["enabled"] is False
        assert body["origin"] == "builtin"
        saved = self.read_saved_config(api_env)
        assert saved["disabled_plugins"]["actions"] == ["open_url"]

    def test_toggle_missing_plugin_rejected(self, api_env):
        body = self.toggle(api_env, "actions", "no_such_plugin").json()
        assert body["ok"] is False

    def test_list_marks_config_disabled_user_plugin(self, api_env):
        self.place_user_plugin(api_env)
        self.toggle(api_env, "actions", "myplug")
        body = api_env.client.get(
            "/api/plugins/list", headers=api_env.headers
        ).json()
        assert body["actions"]["myplug"]["enabled"] is False


class TestPluginUninstall:
    def place_user_plugin(self, api_env, ptype="triggers", pid="myplug"):
        json_name = "trigger.json" if ptype == "triggers" else "action.json"
        plugin_dir = api_env.user_dir / ptype / pid
        plugin_dir.mkdir(parents=True)
        (plugin_dir / json_name).write_text(
            json.dumps(make_meta(ptype, id=pid), ensure_ascii=False),
            encoding="utf-8",
        )
        return plugin_dir

    def test_uninstall_user_plugin(self, api_env):
        plugin_dir = self.place_user_plugin(api_env)
        response = api_env.client.delete(
            "/api/plugins/triggers/myplug", headers=api_env.headers
        )
        assert response.json()["ok"] is True
        assert not plugin_dir.exists()

    def test_uninstall_success(self, api_env):
        self.place_user_plugin(api_env, ptype="actions", pid="actplug")
        response = api_env.client.delete(
            "/api/plugins/actions/actplug", headers=api_env.headers
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "restart_required": True}

    def test_uninstall_builtin_rejected(self, api_env):
        response = api_env.client.delete(
            "/api/plugins/triggers/usb_insert", headers=api_env.headers
        )
        assert response.json()["ok"] is False

    def test_uninstall_nonexistent(self, api_env):
        response = api_env.client.delete(
            "/api/plugins/triggers/ghost", headers=api_env.headers
        )
        assert response.json()["ok"] is False

    def test_bad_ptype(self, api_env):
        response = api_env.client.delete(
            "/api/plugins/widgets/myplug", headers=api_env.headers
        )
        assert response.status_code == 400

    def test_bad_ptype_rejected(self, api_env):
        # 含连续点的插件 id 被安全检查拦截
        response = api_env.client.delete(
            "/api/plugins/triggers/a..b", headers=api_env.headers
        )
        assert response.status_code == 200
        assert response.json()["ok"] is False

    def test_no_auth_rejected(self, api_env):
        response = api_env.client.delete("/api/plugins/triggers/myplug")
        assert response.status_code == 403


class TestInstallSource:
    @staticmethod
    def install_payload(plugin_id="ai_echo"):
        return {
            "kind": "action",
            "plugin_id": plugin_id,
            "manifest": {
                "id": plugin_id,
                "name": "AI 回声",
                "description": "把输入原样返回",
                "enabled": True,
                "version_code": 1,
                "version": "1.0.0",
                "package_name": "com.ai.echo",
                "params": [{"name": "text", "label": "文本", "type": "string"}],
                "outputs": [{"name": "result", "label": "回声", "type": "string"}],
            },
            "source": "def run(action_info, params):\n    return {'result': params.get('text', '')}\n",
        }

    @staticmethod
    def install(api_env, payload):
        return api_env.client.post(
            "/api/plugins/install-source",
            headers=api_env.headers,
            json=payload,
        )

    @staticmethod
    def write_existing_plugin(api_env):
        plugin_dir = api_env.user_dir / "actions" / "ai_echo"
        plugin_dir.mkdir(parents=True)
        old_manifest = '{"id":"ai_echo","version":"old"}'
        old_source = "def run(action_info, params):\n    return {'result': 'old'}\n"
        (plugin_dir / "action.json").write_text(old_manifest, encoding="utf-8")
        (plugin_dir / "action.py").write_text(old_source, encoding="utf-8")
        return plugin_dir, old_manifest, old_source

    def test_installs_without_key_in_permissive_mode(self, api_env, monkeypatch):
        from notmyfault.security.security import SecurityMode

        monkeypatch.setattr(
            api_server, "detect_security_mode", lambda: SecurityMode.PERMISSIVE
        )
        response = self.install(api_env, self.install_payload())
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["id"] == "ai_echo"
        assert data["type"] == "actions"
        assert data["signed"] is False
        assert data["restart_required"] is True

        plugin_dir = api_env.user_dir / "actions" / "ai_echo"
        assert (plugin_dir / "action.json").is_file()
        assert (plugin_dir / "action.py").is_file()
        meta = json.loads((plugin_dir / "action.json").read_text(encoding="utf-8"))
        assert meta["id"] == "ai_echo"
        assert "def run(action_info, params)" in (
            plugin_dir / "action.py"
        ).read_text(encoding="utf-8")

    def test_rejects_manifest_id_mismatch(self, api_env, monkeypatch):
        from notmyfault.security.security import SecurityMode

        monkeypatch.setattr(
            api_server, "detect_security_mode", lambda: SecurityMode.PERMISSIVE
        )
        payload = self.install_payload()
        payload["plugin_id"] = "other_id"
        response = self.install(api_env, payload)
        assert response.status_code == 400
        assert response.json()["ok"] is False
        assert response.json()["code"] == "id_mismatch"

    def test_rejects_source_without_entrypoint(self, api_env, monkeypatch):
        from notmyfault.security.security import SecurityMode

        monkeypatch.setattr(
            api_server, "detect_security_mode", lambda: SecurityMode.PERMISSIVE
        )
        payload = self.install_payload()
        payload["source"] = "x = 1\n"
        response = self.install(api_env, payload)
        assert response.status_code == 400
        assert response.json()["code"] == "missing_entrypoint"

    def test_strict_mode_without_key_rejected(self, api_env, monkeypatch):
        from notmyfault.security.security import SecurityMode

        monkeypatch.setattr(
            api_server, "detect_security_mode", lambda: SecurityMode.STRICT
        )
        response = self.install(api_env, self.install_payload())
        assert response.status_code == 400
        assert response.json()["code"] == "signing_unavailable"

    def test_reinstall_overwrites_files(self, api_env, monkeypatch):
        from notmyfault.security.security import SecurityMode

        monkeypatch.setattr(
            api_server, "detect_security_mode", lambda: SecurityMode.PERMISSIVE
        )
        assert self.install(api_env, self.install_payload()).json()["ok"] is True
        payload = self.install_payload()
        payload["source"] = (
            "def run(action_info, params):\n    return {'result': 'v2'}\n"
        )
        response = self.install(api_env, payload)
        assert response.status_code == 200
        plugin_dir = api_env.user_dir / "actions" / "ai_echo"
        assert "v2" in (plugin_dir / "action.py").read_text(encoding="utf-8")

    def test_write_failure_keeps_existing_plugin(self, api_env, monkeypatch):
        import builtins

        from notmyfault.security.security import SecurityMode

        monkeypatch.setattr(
            api_server, "detect_security_mode", lambda: SecurityMode.PERMISSIVE
        )
        plugin_dir, old_manifest, old_source = self.write_existing_plugin(api_env)
        original_open = builtins.open

        def fail_source_write(path, mode="r", *args, **kwargs):
            if "w" in mode and str(path).endswith("action.py"):
                raise OSError("source write failed")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fail_source_write)
        response = self.install(api_env, self.install_payload())
        assert response.status_code == 500
        assert (plugin_dir / "action.json").read_text(encoding="utf-8") == old_manifest
        assert (plugin_dir / "action.py").read_text(encoding="utf-8") == old_source
        assert sorted(path.name for path in api_env.user_dir.iterdir()) == ["actions"]

    def test_signing_failure_keeps_existing_plugin(self, api_env, monkeypatch):
        from notmyfault.security import signing
        from notmyfault.security.security import SecurityMode

        monkeypatch.setattr(
            api_server, "detect_security_mode", lambda: SecurityMode.STRICT
        )
        write_test_key(api_env.tmp_path / "private")
        plugin_dir, old_manifest, old_source = self.write_existing_plugin(api_env)

        def fail_signing(*args, **kwargs):
            raise RuntimeError("signing failed")

        monkeypatch.setattr(signing, "sign_plugin", fail_signing)
        response = self.install(api_env, self.install_payload())
        assert response.status_code == 500
        assert (plugin_dir / "action.json").read_text(encoding="utf-8") == old_manifest
        assert (plugin_dir / "action.py").read_text(encoding="utf-8") == old_source
        assert sorted(path.name for path in api_env.user_dir.iterdir()) == ["actions"]

    def test_replace_failure_restores_existing_plugin(self, api_env, monkeypatch):
        from notmyfault.security.security import SecurityMode

        monkeypatch.setattr(
            api_server, "detect_security_mode", lambda: SecurityMode.PERMISSIVE
        )
        plugin_dir, old_manifest, old_source = self.write_existing_plugin(api_env)
        original_replace = api_server.os.replace

        def fail_new_directory_replace(source, destination):
            if "-install-" in str(source) and destination == plugin_dir:
                raise OSError("replace failed")
            return original_replace(source, destination)

        monkeypatch.setattr(api_server.os, "replace", fail_new_directory_replace)
        response = self.install(api_env, self.install_payload())
        assert response.status_code == 500
        assert (plugin_dir / "action.json").read_text(encoding="utf-8") == old_manifest
        assert (plugin_dir / "action.py").read_text(encoding="utf-8") == old_source
        assert sorted(path.name for path in api_env.user_dir.iterdir()) == ["actions"]

    def test_no_auth_rejected(self, api_env):
        response = api_env.client.post(
            "/api/plugins/install-source", json=self.install_payload()
        )
        assert response.status_code == 403
