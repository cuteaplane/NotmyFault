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


def build_nmfp(tmp_path, meta, ptype, tag="pkg"):
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
    archive = tmp_path / f"plugin_{tag}.nmfp"
    folder = f"plugin_{tag}"
    with py7zr.SevenZipFile(str(archive), "w") as zf:
        zf.write(source / json_name, f"{folder}/{json_name}")
        zf.write(source / "main.py", f"{folder}/main.py")
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
        assert (dest / "signature.sig").exists()

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
