"""安全模式检测、权限注册表与插件风险扫描"""

from notmyfault.security import security as security_mod
from notmyfault.security.plugin_schema import (
    PERMISSION_REGISTRY,
    PERM_RISK_HIGH,
    PERM_RISK_LOW,
    PERM_RISK_MEDIUM,
    PERM_RISK_NONE,
    check_permissions_conform,
    get_permission_info,
    is_known_permission,
    scan_plugin_security,
    validate_plugin_meta,
)
from notmyfault.security.security import SecurityMode


def make_meta(**overrides):
    meta = {
        "id": "demo",
        "name": "演示插件",
        "description": "演示用插件",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": "com.test.demo",
    }
    meta.update(overrides)
    return meta


class TestSecurityMode:
    def test_security_mode_enum_values(self):
        assert SecurityMode.STRICT.value == "strict"
        assert SecurityMode.NORMAL.value == "normal"
        assert SecurityMode.PERMISSIVE.value == "permissive"

    def test_detect_security_mode_env_alpha(self, monkeypatch):
        monkeypatch.setenv("NOTMYFAULT_MODE", "alpha")
        assert security_mod.detect_security_mode() is SecurityMode.PERMISSIVE

    def test_detect_security_mode_env_dev(self, monkeypatch):
        monkeypatch.setenv("NOTMYFAULT_MODE", "dev")
        assert security_mod.detect_security_mode() is SecurityMode.NORMAL

    def test_detect_security_mode_env_develop(self, monkeypatch):
        monkeypatch.setenv("NOTMYFAULT_MODE", "develop")
        assert security_mod.detect_security_mode() is SecurityMode.NORMAL

    def test_detect_security_mode_env_stable(self, monkeypatch):
        monkeypatch.setenv("NOTMYFAULT_MODE", "stable")
        assert security_mod.detect_security_mode() is SecurityMode.STRICT

    def test_detect_security_mode_env_master(self, monkeypatch):
        monkeypatch.setenv("NOTMYFAULT_MODE", "master")
        assert security_mod.detect_security_mode() is SecurityMode.STRICT

    def test_detect_security_mode_env_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("NOTMYFAULT_MODE", "  ALPHA ")
        assert security_mod.detect_security_mode() is SecurityMode.PERMISSIVE
        monkeypatch.setenv("NOTMYFAULT_MODE", "Develop")
        assert security_mod.detect_security_mode() is SecurityMode.NORMAL

    def test_detect_security_mode_default_strict(self, tmp_path, monkeypatch):
        # 项目根与 cwd 下都可能存在签名的 build.json，隔离后才是真正的默认路径
        monkeypatch.delenv("NOTMYFAULT_MODE", raising=False)
        monkeypatch.setattr(security_mod, "_PROJECT_ROOT", tmp_path)
        monkeypatch.chdir(tmp_path)
        assert security_mod.detect_security_mode() is SecurityMode.STRICT


class TestPermissionRegistry:
    def test_all_registered_permissions_have_required_fields(self):
        assert PERMISSION_REGISTRY
        for perm, info in PERMISSION_REGISTRY.items():
            assert isinstance(perm, str)
            assert info["label"]
            assert info["risk"]
            assert info["description"]

    def test_get_permission_info_returns_correct_data(self):
        info = get_permission_info("network")
        assert info is PERMISSION_REGISTRY["network"]
        assert info["label"] == "网络访问"
        assert info["risk"] == PERM_RISK_MEDIUM

    def test_get_permission_info_unknown_returns_none(self):
        assert get_permission_info("teleport") is None

    def test_is_known_permission(self):
        assert is_known_permission("admin") is True
        assert is_known_permission("notification") is True
        assert is_known_permission("root") is False

    def test_risk_levels_are_valid(self):
        valid_levels = {PERM_RISK_NONE, PERM_RISK_LOW, PERM_RISK_MEDIUM, PERM_RISK_HIGH}
        for info in PERMISSION_REGISTRY.values():
            assert info["risk"] in valid_levels


class TestCheckPermissionsConform:
    def test_empty_permissions_pass(self):
        ok, errors = check_permissions_conform([])
        assert ok is True
        assert errors == []

    def test_valid_permissions_pass(self):
        ok, errors = check_permissions_conform(["notification", "network", "admin"])
        assert ok is True
        assert errors == []

    def test_unknown_permission_fails(self):
        ok, errors = check_permissions_conform(["teleport"])
        assert ok is False
        assert errors == ["未知权限: teleport"]

    def test_multiple_unknown_permissions_listed(self):
        ok, errors = check_permissions_conform(["foo", "notification", "bar"])
        assert ok is False
        assert errors == ["未知权限: foo", "未知权限: bar"]


class TestScanPluginSecurity:
    def _scan(self, tmp_path, source):
        (tmp_path / "action.py").write_text(source, encoding="utf-8")
        return scan_plugin_security(str(tmp_path))

    def test_clean_plugin_no_risks(self, tmp_path):
        risks = self._scan(tmp_path, "def run(params):\n    return True\n")
        assert risks == []

    def test_detect_code_injection(self, tmp_path):
        risks = self._scan(tmp_path, "result = eval(user_input)\n")
        assert any(r["id"] == "code_injection" and r["level"] == PERM_RISK_HIGH for r in risks)

    def test_detect_subprocess(self, tmp_path):
        risks = self._scan(tmp_path, "import subprocess\nsubprocess.run(['dir'])\n")
        assert any(r["id"] == "subprocess" and r["level"] == PERM_RISK_HIGH for r in risks)

    def test_detect_file_write(self, tmp_path):
        risks = self._scan(tmp_path, "with open('out.txt', 'w') as f:\n    f.write('x')\n")
        assert any(r["id"] == "file_write" and r["level"] == PERM_RISK_MEDIUM for r in risks)

    def test_detect_network_request(self, tmp_path):
        risks = self._scan(tmp_path, "import requests\nrequests.get('http://example.test')\n")
        assert any(r["id"] == "network_request" and r["level"] == PERM_RISK_MEDIUM for r in risks)

    def test_detect_native_call(self, tmp_path):
        risks = self._scan(tmp_path, "import ctypes\nctypes.windll.user32.GetForegroundWindow()\n")
        assert any(r["id"] == "native_call" and r["level"] == PERM_RISK_MEDIUM for r in risks)

    def test_detect_dynamic_import(self, tmp_path):
        risks = self._scan(tmp_path, "import importlib\nimportlib.import_module(name)\n")
        assert any(r["id"] == "dynamic_import" for r in risks)

    def test_detect_registry_access(self, tmp_path):
        risks = self._scan(tmp_path, "import winreg\nwinreg.OpenKey(root, name)\n")
        assert any(r["id"] == "registry_access" for r in risks)

    def test_resolves_import_aliases(self, tmp_path):
        risks = self._scan(
            tmp_path,
            "import subprocess as process\nprocess.run(['tool'])\n",
        )
        assert any(r["id"] == "subprocess" for r in risks)

    def test_comments_and_strings_do_not_create_risks(self, tmp_path):
        risks = self._scan(
            tmp_path,
            "# subprocess.run(['tool'])\n"
            "message = \"eval(user_input) requests.get(url)\"\n",
        )
        assert risks == []

    def test_nonexistent_directory_returns_empty(self):
        assert scan_plugin_security("/path/does/not/exist") == []

    def test_risk_levels_match_registry(self, tmp_path):
        source = (
            "import ctypes, subprocess\n"
            "eval('1')\n"
            "open('x', 'w')\n"
        )
        risks = self._scan(tmp_path, source)
        assert risks
        valid_levels = {PERM_RISK_NONE, PERM_RISK_LOW, PERM_RISK_MEDIUM, PERM_RISK_HIGH}
        for risk in risks:
            assert risk["level"] in valid_levels
            assert risk["file"] == "action.py"
            assert "detail" in risk


class TestValidatePluginMetaSecurity:
    def test_accepts_known_permissions(self):
        meta = make_meta(permissions=["notification", "network", "clipboard"])
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is True
        assert errors == []

    def test_rejects_unknown_permission(self):
        meta = make_meta(permissions=["root"])
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert any("未知权限类型: 'root'" in e for e in errors)

    def test_rejects_invalid_platform(self):
        meta = make_meta(platforms=["windows", "amiga"])
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert any("无效平台" in e and "amiga" in e for e in errors)
