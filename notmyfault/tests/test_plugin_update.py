"""插件安装的更新语义：分类、diff、备份和回滚"""

import json
import shutil

from fastapi.testclient import TestClient

from notmyfault.tests.test_api_plugins import (
    FakeRunner,
    build_nmfp,
    install_file,
    make_meta,
    upload_preview,
)


def make_env(tmp_path, monkeypatch):
    import notmyfault.config as config_mod
    from notmyfault.host import api_server
    from notmyfault.host.api_server import EngineAPI

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
    return api, client, headers, user_dir


def action_meta(**overrides):
    meta = make_meta(
        "actions",
        id="update_demo",
        package_name="com.test.update_demo",
        permissions=["notification"],
    )
    meta.update(overrides)
    return meta


class TestClassify:
    def test_new_install(self, tmp_path, monkeypatch):
        api, client, headers, _ = make_env(tmp_path, monkeypatch)
        result = api._classify_update(action_meta())
        assert result["kind"] == "new"

    def test_upgrade_and_downgrade_and_reinstall(self, tmp_path, monkeypatch):
        api, client, headers, user_dir = make_env(tmp_path, monkeypatch)
        v1 = build_nmfp(tmp_path, action_meta(version_code=1), "actions", tag="v1")
        assert install_file(client, headers, v1).json()["ok"] is True

        assert api._classify_update(action_meta(version_code=2))["kind"] == "upgrade"
        assert api._classify_update(action_meta(version_code=1))["kind"] == "reinstall"
        assert api._classify_update(action_meta(version_code=0))["kind"] == "downgrade"

    def test_identity_uses_package_name_not_folder(self, tmp_path, monkeypatch):
        # 目录名变了还是同一个包，按 package_name 认出来
        api, client, headers, user_dir = make_env(tmp_path, monkeypatch)
        v1 = build_nmfp(tmp_path, action_meta(version_code=1), "actions", tag="a1")
        assert install_file(client, headers, v1).json()["ok"] is True

        renamed = action_meta(version_code=2, id="update_demo_renamed")
        v2 = build_nmfp(tmp_path, renamed, "actions", tag="a2")
        result = api._classify_update(renamed)
        assert result["kind"] == "upgrade"
        assert result["installed_id"] == "update_demo"


class TestPreviewDiff:
    def test_preview_diff_permissions_and_capabilities(self, tmp_path, monkeypatch):
        api, client, headers, _ = make_env(tmp_path, monkeypatch)
        v1 = build_nmfp(
            tmp_path,
            action_meta(
                version_code=1,
                permissions=["notification", "filesystem"],
                requires_capabilities=["clipboard.read"],
            ),
            "actions",
            tag="d1",
        )
        assert install_file(client, headers, v1).json()["ok"] is True

        v2 = build_nmfp(
            tmp_path,
            action_meta(
                version_code=2,
                permissions=["notification", "process"],
                requires_capabilities=["clipboard.read", "display.brightness"],
            ),
            "actions",
            tag="d2",
        )
        response = upload_preview(client, headers, v2)
        body = response.json()
        assert body["ok"] is True
        diff = body["update_diff"]
        assert diff["update"]["kind"] == "upgrade"
        assert diff["permission_diff"] == {"added": ["process"], "removed": ["filesystem"]}
        assert diff["capability_diff"] == {
            "added": ["display.brightness"],
            "removed": [],
        }
        assert diff["signature_identity_changed"] is False

    def test_preview_flags_signature_identity_change(self, tmp_path, monkeypatch):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from notmyfault.security.signing import self_sign_plugin

        api, client, headers, _ = make_env(tmp_path, monkeypatch)
        # 官方签名装 v1，作者自签的 v2 进来时签名身份要标出来
        v1 = build_nmfp(tmp_path, action_meta(version_code=1), "actions", tag="s1")
        assert install_file(client, headers, v1).json()["ok"] is True

        v2 = build_nmfp(
            tmp_path, action_meta(version_code=2), "actions", tag="s2", sign=True
        )
        body = upload_preview(client, headers, v2).json()
        assert body["update_diff"]["signature_identity_changed"] is True


class TestBackupAndRollback:
    def test_successful_update_keeps_one_backup(self, tmp_path, monkeypatch):
        api, client, headers, user_dir = make_env(tmp_path, monkeypatch)
        v1 = build_nmfp(tmp_path, action_meta(version_code=1), "actions", tag="b1")
        assert install_file(client, headers, v1).json()["ok"] is True

        v2 = build_nmfp(tmp_path, action_meta(version_code=2), "actions", tag="b2")
        result = install_file(client, headers, v2).json()
        assert result["ok"] is True
        assert result["backup_kept"] is True

        backup = user_dir / "actions" / "update_demo.nmf-backup"
        assert backup.is_dir()
        backup_meta = json.loads((backup / "action.json").read_text(encoding="utf-8"))
        assert backup_meta["version_code"] == 1

        # 再装一版，备份只剩最近一份（v2）
        v3 = build_nmfp(tmp_path, action_meta(version_code=3), "actions", tag="b3")
        assert install_file(client, headers, v3).json()["ok"] is True
        backup_meta = json.loads((backup / "action.json").read_text(encoding="utf-8"))
        assert backup_meta["version_code"] == 2

    def test_rollback_on_written_manifest_corruption(self, tmp_path, monkeypatch):
        api, client, headers, user_dir = make_env(tmp_path, monkeypatch)
        v1 = build_nmfp(tmp_path, action_meta(version_code=1), "actions", tag="r1")
        assert install_file(client, headers, v1).json()["ok"] is True

        # 直接把备份还原逻辑对上：写坏的目标目录在落盘校验时会被回滚
        plugin_dir = user_dir / "actions" / "update_demo"
        backup = api._backup_plugin(str(user_dir), "actions", "update_demo")
        assert backup is not None
        # 模拟 copytree 装了一半失败后的状态
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "action.json").write_text("{broken", encoding="utf-8")
        api._rollback_plugin([backup])
        restored = json.loads((plugin_dir / "action.json").read_text(encoding="utf-8"))
        assert restored["version_code"] == 1
        assert not (user_dir / "actions" / "update_demo.nmf-backup").exists()

    def test_scan_ignores_backup_directories(self, tmp_path, monkeypatch):
        api, client, headers, user_dir = make_env(tmp_path, monkeypatch)
        v1 = build_nmfp(tmp_path, action_meta(version_code=1), "actions", tag="i1")
        assert install_file(client, headers, v1).json()["ok"] is True
        v2 = build_nmfp(tmp_path, action_meta(version_code=2), "actions", tag="i2")
        assert install_file(client, headers, v2).json()["ok"] is True

        from notmyfault.security.plugin_schema import scan_plugins

        found = scan_plugins(str(user_dir), "actions", "action.json")
        assert "update_demo" in found
        assert not any(pid.endswith("nmf-backup") for pid in found)

        # 分类也认的是主目录，不会把备份当已安装版本
        assert api._classify_update(action_meta(version_code=3))["kind"] == "upgrade"


class TestBuildHookGate:
    def test_install_reports_backup_kept_field(self, tmp_path, monkeypatch):
        api, client, headers, user_dir = make_env(tmp_path, monkeypatch)
        v1 = build_nmfp(tmp_path, action_meta(version_code=1), "actions", tag="k1")
        assert install_file(client, headers, v1).json()["ok"] is True
        v2 = build_nmfp(tmp_path, action_meta(version_code=2), "actions", tag="k2")
        body = install_file(client, headers, v2).json()
        assert body["ok"] is True
        assert body["backup_kept"] is True
