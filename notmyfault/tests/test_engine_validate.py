"""插件元数据校验、sudo 导入检查和 AST 能力扫描"""

import pytest

from notmyfault.security.plugin_schema import validate_plugin_meta
from notmyfault.security.plugins import check_sudo_import, scan_plugin_capabilities


def write_source(tmp_path, source, name="plugin.py"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return str(path)


class TestCheckSudoImport:
    def test_import_direct(self, tmp_path):
        path = write_source(tmp_path, "import notmyfault.security.sudo\n")
        assert check_sudo_import(path) is True

    def test_import_direct_with_alias(self, tmp_path):
        path = write_source(tmp_path, "import notmyfault.security.sudo as s\n")
        assert check_sudo_import(path) is True

    def test_import_from_direct(self, tmp_path):
        path = write_source(tmp_path, "from notmyfault.security.sudo import run_as_admin\n")
        assert check_sudo_import(path) is True

    def test_import_from_notmyfault(self, tmp_path):
        path = write_source(tmp_path, "from notmyfault import sudo\n")
        assert check_sudo_import(path) is True

    def test_import_from_notmyfault_multiple(self, tmp_path):
        path = write_source(tmp_path, "from notmyfault import config, sudo\n")
        assert check_sudo_import(path) is True

    def test_import_from_security_package(self, tmp_path):
        path = write_source(tmp_path, "from notmyfault.security import sudo\n")
        assert check_sudo_import(path) is True

    def test_import_security_package_alias(self, tmp_path):
        path = write_source(tmp_path, "import notmyfault.security as sec\n")
        assert check_sudo_import(path) is True

    def test_import_in_function_body(self, tmp_path):
        path = write_source(
            tmp_path,
            "def run(meta, params):\n    import notmyfault.security.sudo\n",
        )
        assert check_sudo_import(path) is True

    def test_import_other_notmyfault_module(self, tmp_path):
        path = write_source(tmp_path, "import notmyfault.config\n")
        assert check_sudo_import(path) is False

    def test_no_sudo_import(self, tmp_path):
        path = write_source(tmp_path, "import os\nimport sys\n")
        assert check_sudo_import(path) is False

    def test_no_import_but_mention_in_comment(self, tmp_path):
        path = write_source(tmp_path, "# import notmyfault.security.sudo\n")
        assert check_sudo_import(path) is False

    def test_no_import_but_mention_in_string(self, tmp_path):
        path = write_source(tmp_path, "text = 'import notmyfault.security.sudo'\n")
        assert check_sudo_import(path) is False

    def test_syntax_error_in_source(self, tmp_path):
        path = write_source(tmp_path, "def (\n")
        assert check_sudo_import(path) is False

    def test_file_not_found(self, tmp_path):
        assert check_sudo_import(str(tmp_path / "missing.py")) is False


class TestScanPluginCapabilities:
    def test_clean_code_no_caps(self, tmp_path):
        path = write_source(tmp_path, "def run(meta, params):\n    return 1 + 1\n")
        assert scan_plugin_capabilities(path) == set()

    def test_import_subprocess(self, tmp_path):
        path = write_source(tmp_path, "import subprocess\n")
        assert scan_plugin_capabilities(path) == {"external_binary"}

    def test_import_ctypes(self, tmp_path):
        path = write_source(tmp_path, "import ctypes\n")
        assert scan_plugin_capabilities(path) == {"native_api"}

    def test_import_win32api(self, tmp_path):
        path = write_source(tmp_path, "import win32api\n")
        assert scan_plugin_capabilities(path) == {"native_api"}

    def test_from_subprocess_import_run(self, tmp_path):
        path = write_source(tmp_path, "from subprocess import run\nrun(['dir'])\n")
        assert "external_binary" in scan_plugin_capabilities(path)

    def test_from_os_import_system(self, tmp_path):
        path = write_source(tmp_path, "from os import system\nsystem('dir')\n")
        assert "external_binary" in scan_plugin_capabilities(path)

    def test_from_os_import_popen(self, tmp_path):
        path = write_source(tmp_path, "from os import popen\npopen('dir')\n")
        assert "external_binary" in scan_plugin_capabilities(path)

    def test_from_os_import_path_not_flagged(self, tmp_path):
        path = write_source(tmp_path, "from os import path\npath.join('a', 'b')\n")
        assert scan_plugin_capabilities(path) == set()

    def test_from_ctypes_import_windll(self, tmp_path):
        path = write_source(tmp_path, "from ctypes import windll\n")
        assert "native_api" in scan_plugin_capabilities(path)

    def test_os_system(self, tmp_path):
        path = write_source(tmp_path, "import os\nos.system('dir')\n")
        assert "external_binary" in scan_plugin_capabilities(path)

    def test_os_popen(self, tmp_path):
        path = write_source(tmp_path, "import os\nos.popen('dir')\n")
        assert "external_binary" in scan_plugin_capabilities(path)

    def test_subprocess_run_call(self, tmp_path):
        path = write_source(tmp_path, "import subprocess\nsubprocess.run(['dir'])\n")
        assert "external_binary" in scan_plugin_capabilities(path)

    def test_subprocess_popen_call(self, tmp_path):
        path = write_source(tmp_path, "import subprocess\nsubprocess.Popen(['dir'])\n")
        assert "external_binary" in scan_plugin_capabilities(path)

    def test_ctypes_windll_attr(self, tmp_path):
        path = write_source(tmp_path, "import ctypes\nctypes.windll.user32\n")
        assert "native_api" in scan_plugin_capabilities(path)

    def test_shell_true_keyword(self, tmp_path):
        path = write_source(
            tmp_path, "import subprocess\nsubprocess.run('dir', shell=True)\n"
        )
        assert "external_binary" in scan_plugin_capabilities(path)

    def test_shell_true_keyword_alone(self, tmp_path):
        # 没有 subprocess 导入时 shell=True 关键字本身也要标记
        path = write_source(tmp_path, "launch('dir', shell=True)\n")
        assert "external_binary" in scan_plugin_capabilities(path)

    def test_shell_false_not_flagged(self, tmp_path):
        path = write_source(tmp_path, "launch('dir', shell=False)\n")
        assert scan_plugin_capabilities(path) == set()

    def test_import_in_function_body(self, tmp_path):
        path = write_source(
            tmp_path, "def run(meta, params):\n    import subprocess\n"
        )
        assert "external_binary" in scan_plugin_capabilities(path)

    def test_both_caps_together(self, tmp_path):
        path = write_source(
            tmp_path,
            "import ctypes\nimport subprocess\n"
            "ctypes.windll.user32\nsubprocess.run(['dir'])\n",
        )
        caps = scan_plugin_capabilities(path)
        assert "native_api" in caps
        assert "external_binary" in caps

    def test_comment_mention_not_detected(self, tmp_path):
        path = write_source(tmp_path, "# subprocess.run(['dir'])\n")
        assert scan_plugin_capabilities(path) == set()

    def test_string_mention_not_detected(self, tmp_path):
        path = write_source(tmp_path, "text = \"subprocess.run(['dir'])\"\n")
        assert scan_plugin_capabilities(path) == set()

    def test_syntax_error_returns_empty(self, tmp_path):
        path = write_source(tmp_path, "def (\n")
        assert scan_plugin_capabilities(path) == set()

    def test_nonexistent_file_returns_empty(self, tmp_path):
        assert scan_plugin_capabilities(str(tmp_path / "missing.py")) == set()


def make_meta(**overrides):
    meta = {
        "id": "plug_a",
        "name": "测试插件",
        "description": "测试用插件",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": "com.test.plug",
    }
    meta.update(overrides)
    return meta


class TestValidatePluginMeta:
    def test_minimal_valid_meta(self):
        ok, errors = validate_plugin_meta(make_meta(), "action")
        assert ok is True
        assert errors == []

    def test_valid_action_meta(self):
        meta = make_meta(
            params=[{"name": "text", "type": "string", "label": "文本"}],
            permissions=["clipboard"],
        )
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is True

    def test_valid_trigger_meta(self):
        meta = make_meta(trigger_api="event-v2")
        ok, errors = validate_plugin_meta(meta, "trigger")
        assert ok is True

    def test_valid_trigger_oneshot_semantic(self):
        ok, errors = validate_plugin_meta(make_meta(semantic="oneshot"), "trigger")
        assert ok is True

    def test_valid_trigger_state_semantic(self):
        ok, errors = validate_plugin_meta(make_meta(semantic="state"), "trigger")
        assert ok is True

    def test_not_a_dict_none(self):
        ok, errors = validate_plugin_meta(None, "action")
        assert ok is False
        assert errors == ["插件元数据不是有效的 JSON 对象"]

    def test_not_a_dict_string(self):
        ok, errors = validate_plugin_meta("meta", "action")
        assert ok is False
        assert errors == ["插件元数据不是有效的 JSON 对象"]

    def test_not_a_dict_int(self):
        ok, errors = validate_plugin_meta(42, "action")
        assert ok is False

    def test_missing_all_required(self):
        ok, errors = validate_plugin_meta({}, "action")
        assert ok is False
        missing = [e for e in errors if e.startswith("缺少必填字段")]
        assert len(missing) == 7

    def test_missing_single_field_id(self):
        meta = make_meta()
        del meta["id"]
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert "缺少必填字段: id" in errors

    def test_missing_name(self):
        meta = make_meta()
        del meta["name"]
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert "缺少必填字段: name" in errors

    def test_missing_description(self):
        meta = make_meta()
        del meta["description"]
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert "缺少必填字段: description" in errors

    def test_missing_enabled(self):
        meta = make_meta()
        del meta["enabled"]
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert "缺少必填字段: enabled" in errors

    def test_missing_version_code(self):
        meta = make_meta()
        del meta["version_code"]
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert "缺少必填字段: version_code" in errors

    def test_id_not_string(self):
        ok, errors = validate_plugin_meta(make_meta(id=123), "action")
        assert ok is False
        assert any("'id' 必须是字符串" in e for e in errors)

    def test_name_not_string(self):
        ok, errors = validate_plugin_meta(make_meta(name=1), "action")
        assert ok is False
        assert any("'name' 必须是字符串" in e for e in errors)

    def test_description_not_string(self):
        ok, errors = validate_plugin_meta(make_meta(description=None), "action")
        assert ok is False
        assert any("'description' 必须是字符串" in e for e in errors)

    def test_enabled_is_false(self):
        ok, errors = validate_plugin_meta(make_meta(enabled=False), "action")
        assert ok is True

    def test_enabled_not_bool_string(self):
        ok, errors = validate_plugin_meta(make_meta(enabled="yes"), "action")
        assert ok is False
        assert any("'enabled' 必须为布尔值" in e for e in errors)

    def test_enabled_not_bool_int(self):
        ok, errors = validate_plugin_meta(make_meta(enabled=1), "action")
        assert ok is False
        assert any("'enabled' 必须为布尔值" in e for e in errors)

    def test_version_code_not_int_string(self):
        ok, errors = validate_plugin_meta(make_meta(version_code="1"), "action")
        assert ok is False
        assert any("'version_code' 必须为整数" in e for e in errors)

    def test_version_code_not_int_float(self):
        ok, errors = validate_plugin_meta(make_meta(version_code=1.5), "action")
        assert ok is False
        assert any("'version_code' 必须为整数" in e for e in errors)

    def test_version_code_is_zero(self):
        ok, errors = validate_plugin_meta(make_meta(version_code=0), "action")
        assert ok is False
        assert any("必须 >= 1" in e for e in errors)

    def test_bad_types_everywhere(self):
        meta = make_meta(id=1, name=2, description=3, enabled="x", version_code="y")
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert len(errors) >= 4

    def test_multiple_errors_accumulate(self):
        meta = make_meta(name=1, enabled="x", version_code="y")
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert len(errors) == 3

    def test_permissions_not_list(self):
        ok, errors = validate_plugin_meta(make_meta(permissions="admin"), "action")
        assert ok is False
        assert any("'permissions' 必须是数组" in e for e in errors)

    def test_permissions_empty_list(self):
        ok, errors = validate_plugin_meta(make_meta(permissions=[]), "action")
        assert ok is True

    def test_permissions_unknown_value(self):
        ok, errors = validate_plugin_meta(make_meta(permissions=["teleport"]), "action")
        assert ok is False
        assert any("未知权限类型" in e for e in errors)

    def test_permissions_non_string_element(self):
        ok, errors = validate_plugin_meta(make_meta(permissions=[123]), "action")
        assert ok is False
        assert any("permissions 中的值必须是字符串" in e for e in errors)

    def test_permissions_mixed_valid_and_invalid(self):
        ok, errors = validate_plugin_meta(
            make_meta(permissions=["clipboard", "teleport"]), "action"
        )
        assert ok is False
        assert sum("未知权限类型" in e for e in errors) == 1

    def test_action_with_admin_permission(self):
        ok, errors = validate_plugin_meta(make_meta(permissions=["admin"]), "action")
        assert ok is True

    def test_params_not_list(self):
        ok, errors = validate_plugin_meta(make_meta(params="x"), "action")
        assert ok is False
        assert any("'params' 必须是数组" in e for e in errors)

    def test_params_empty_list(self):
        ok, errors = validate_plugin_meta(make_meta(params=[]), "action")
        assert ok is True

    def test_param_item_not_dict(self):
        ok, errors = validate_plugin_meta(make_meta(params=["x"]), "action")
        assert ok is False
        assert any("params[0] 必须是对象" in e for e in errors)

    def test_param_missing_name(self):
        param = {"type": "string", "label": "文本"}
        ok, errors = validate_plugin_meta(make_meta(params=[param]), "action")
        assert ok is False
        assert any("缺少必填字段: name" in e for e in errors)

    def test_param_missing_type(self):
        param = {"name": "text", "label": "文本"}
        ok, errors = validate_plugin_meta(make_meta(params=[param]), "action")
        assert ok is False
        assert any("缺少必填字段: type" in e for e in errors)

    def test_param_missing_label(self):
        param = {"name": "text", "type": "string"}
        ok, errors = validate_plugin_meta(make_meta(params=[param]), "action")
        assert ok is False
        assert any("缺少必填字段: label" in e for e in errors)

    def test_param_type_invalid(self):
        param = {"name": "text", "type": "slider", "label": "文本"}
        ok, errors = validate_plugin_meta(make_meta(params=[param]), "action")
        assert ok is False
        assert any("type 无效" in e for e in errors)

    def test_param_all_valid_types(self):
        params = [
            {"name": f"p{i}", "type": t, "label": t}
            for i, t in enumerate(
                ["string", "number", "bool", "time", "hotkey", "path", "textarea"]
            )
        ]
        params.append({
            "name": "sel", "type": "select", "label": "选项",
            "options": ["a", "b"],
        })
        ok, errors = validate_plugin_meta(make_meta(params=params), "action")
        assert ok is True

    def test_select_param_missing_options(self):
        param = {"name": "mode", "type": "select", "label": "模式"}
        ok, errors = validate_plugin_meta(make_meta(params=[param]), "action")
        assert ok is False
        assert any("必须提供 'options' 字段" in e for e in errors)

    def test_select_param_with_options(self):
        param = {
            "name": "mode", "type": "select", "label": "模式",
            "options": [{"value": "a", "label": "A"}, "b"],
        }
        ok, errors = validate_plugin_meta(make_meta(params=[param]), "action")
        assert ok is True

    def test_semantic_invalid_value(self):
        ok, errors = validate_plugin_meta(make_meta(semantic="continuous"), "trigger")
        assert ok is False
        assert any("'semantic' 无效" in e for e in errors)

    def test_semantic_case_sensitive(self):
        ok, errors = validate_plugin_meta(make_meta(semantic="State"), "trigger")
        assert ok is False
        assert any("'semantic' 无效" in e for e in errors)

    def test_semantic_empty_string(self):
        ok, errors = validate_plugin_meta(make_meta(semantic=""), "trigger")
        assert ok is False
        assert any("'semantic' 无效" in e for e in errors)

    def test_semantic_on_action_is_unknown(self):
        # semantic 只对触发器有意义，动作清单里属于未知字段
        ok, errors = validate_plugin_meta(make_meta(semantic="oneshot"), "action")
        assert ok is False
        assert any("包含未知字段: 'semantic'" in e for e in errors)

    def test_unknown_field_in_trigger(self):
        ok, errors = validate_plugin_meta(make_meta(foo="bar"), "trigger")
        assert ok is False
        assert any("包含未知字段: 'foo'" in e for e in errors)
