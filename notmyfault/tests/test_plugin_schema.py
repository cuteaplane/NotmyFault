"""插件 schema：权限注册表、风险扫描、元数据校验与目录扫描"""

import json

from notmyfault.security import plugin_schema
from notmyfault.security.plugin_schema import (
    PERMISSION_REGISTRY,
    PERM_RISK_HIGH,
    PERM_RISK_LOW,
    PERM_RISK_MEDIUM,
    PERM_RISK_NONE,
    check_permissions_conform,
    current_platform_name,
    get_permission_info,
    is_known_permission,
    scan_plugin_security,
    scan_plugins,
    validate_plugin_meta,
)


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


def write_plugin(tmp_path, source):
    (tmp_path / "action.py").write_text(source, encoding="utf-8")
    return str(tmp_path)


class TestPermissionRegistry:
    def test_core_permissions_exist(self):
        for perm in ("notification", "network", "clipboard", "filesystem", "admin"):
            assert perm in PERMISSION_REGISTRY

    def test_all_permissions_have_valid_risk(self):
        valid = {PERM_RISK_NONE, PERM_RISK_LOW, PERM_RISK_MEDIUM, PERM_RISK_HIGH}
        for info in PERMISSION_REGISTRY.values():
            assert set(info) == {"label", "risk", "description"}
            assert info["risk"] in valid

    def test_get_permission_info_known(self):
        info = get_permission_info("admin")
        assert info["label"] == "管理员权限"
        assert info["risk"] == PERM_RISK_HIGH

    def test_get_permission_info_unknown(self):
        assert get_permission_info("does_not_exist") is None

    def test_is_known_permission(self):
        assert is_known_permission("process") is True
        assert is_known_permission("superuser") is False


class TestCheckPermissionsConform:
    def test_empty_permissions(self):
        assert check_permissions_conform([]) == (True, [])

    def test_all_known_permissions(self):
        ok, errors = check_permissions_conform(sorted(PERMISSION_REGISTRY))
        assert ok is True
        assert errors == []

    def test_with_unknown_permissions(self):
        ok, errors = check_permissions_conform(["network", "teleport"])
        assert ok is False
        assert errors == ["未知权限: teleport"]

    def test_all_unknown(self):
        ok, errors = check_permissions_conform(["a", "b"])
        assert ok is False
        assert errors == ["未知权限: a", "未知权限: b"]


class TestScanPluginSecurity:
    def test_clean_plugin_no_risks(self, tmp_path):
        assert scan_plugin_security(write_plugin(tmp_path, "X = 1\n")) == []

    def test_detect_code_injection(self, tmp_path):
        risks = scan_plugin_security(write_plugin(tmp_path, "eval(data)\n"))
        assert [r["id"] for r in risks] == ["code_injection"]
        assert risks[0]["level"] == PERM_RISK_HIGH

    def test_detect_subprocess(self, tmp_path):
        risks = scan_plugin_security(write_plugin(tmp_path, "subprocess.run(['dir'])\n"))
        assert any(r["id"] == "subprocess" for r in risks)

    def test_detect_native_call(self, tmp_path):
        risks = scan_plugin_security(write_plugin(tmp_path, "ctypes.windll.user32.MessageBoxW\n"))
        assert any(r["id"] == "native_call" and r["level"] == PERM_RISK_MEDIUM for r in risks)

    def test_empty_directory(self, tmp_path):
        assert scan_plugin_security(str(tmp_path)) == []

    def test_nonexistent_directory(self):
        assert scan_plugin_security("/no/such/dir") == []

    def test_multiple_py_files(self, tmp_path):
        (tmp_path / "a.py").write_text("subprocess.run(['dir'])\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("ctypes.windll.user32.MessageBoxW\n", encoding="utf-8")
        risks = scan_plugin_security(str(tmp_path))
        files = {r["file"] for r in risks}
        assert "a.py" in files
        assert "b.py" in files

    def test_scan_ignores_json_files(self, tmp_path):
        (tmp_path / "action.json").write_text('{"danger": "eval("}', encoding="utf-8")
        assert scan_plugin_security(str(tmp_path)) == []


class TestScanPlugins:
    def test_basic_scan_triggers(self, tmp_path):
        folder = tmp_path / "triggers" / "demo"
        folder.mkdir(parents=True)
        meta = make_meta(id="demo")
        (folder / "trigger.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        (folder / "trigger.py").write_text("def run():\n    pass\n", encoding="utf-8")

        found = scan_plugins(str(tmp_path), "triggers", "trigger.json")
        assert set(found) == {"demo"}
        entry = found["demo"]
        assert entry["platform_compatible"] is True
        assert entry["current_platform"] == current_platform_name()
        assert entry["selected_entrypoint"] is None

    def test_disabled_plugin_skipped(self, tmp_path):
        folder = tmp_path / "triggers" / "demo"
        folder.mkdir(parents=True)
        meta = make_meta(id="demo", enabled=False)
        (folder / "trigger.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        assert scan_plugins(str(tmp_path), "triggers", "trigger.json") == {}


class TestValidatePluginMetaPermissions:
    def test_uia_selector_param_type_is_valid(self):
        ok, errors = validate_plugin_meta(
            make_meta(params=[{
                "name": "target",
                "type": "uia_selector",
                "label": "屏幕控件",
                "value_type": "object",
            }]),
            "action",
        )
        assert ok is True
        assert errors == []

    def test_param_required_flag_is_boolean(self):
        ok, errors = validate_plugin_meta(
            make_meta(params=[{
                "name": "target",
                "type": "uia_selector",
                "label": "屏幕控件",
                "required": "yes",
            }]),
            "action",
        )
        assert ok is False
        assert "params[0].required 必须为布尔值" in errors

    def test_action_cancellation_contract_requires_context_execution(self):
        ok, errors = validate_plugin_meta(
            make_meta(
                execution_api="context-v1",
                cancellation_api="runtime-v1",
            ),
            "action",
        )
        assert ok is True
        assert errors == []

        ok, errors = validate_plugin_meta(
            make_meta(cancellation_api="runtime-v1"), "action"
        )
        assert ok is False
        assert (
            "cancellation_api=runtime-v1 需要 execution_api=context-v1"
            in errors
        )

    def test_action_cancellation_contract_rejects_unknown_version(self):
        ok, errors = validate_plugin_meta(
            make_meta(
                execution_api="context-v1",
                cancellation_api="runtime-v2",
            ),
            "action",
        )
        assert ok is False
        assert "cancellation_api 目前仅支持 runtime-v1" in errors

    def test_action_idempotent_flag_is_boolean(self):
        ok, errors = validate_plugin_meta(make_meta(idempotent=True), "action")
        assert ok is True
        assert errors == []

        ok, errors = validate_plugin_meta(make_meta(idempotent="yes"), "action")
        assert ok is False
        assert "字段 'idempotent' 必须是布尔值" in errors

    def test_valid_permissions(self):
        ok, errors = validate_plugin_meta(
            make_meta(permissions=["network", "filesystem"]), "action"
        )
        assert ok is True
        assert errors == []

    def test_unknown_permissions(self):
        ok, errors = validate_plugin_meta(make_meta(permissions=["warp"]), "action")
        assert ok is False
        assert any("未知权限类型: 'warp'" in e for e in errors)

    def test_permissions_not_list(self):
        ok, errors = validate_plugin_meta(make_meta(permissions="network"), "action")
        assert ok is False
        assert any("'permissions' 必须是数组" in e for e in errors)

    def test_permission_item_not_string(self):
        ok, errors = validate_plugin_meta(make_meta(permissions=[42]), "action")
        assert ok is False
        assert any("permissions 中的值必须是字符串" in e for e in errors)

    def test_origin_field(self):
        for origin in ("builtin", "user", "third_party"):
            ok, errors = validate_plugin_meta(make_meta(origin=origin), "action")
            assert ok is True, errors

    def test_origin_invalid(self):
        ok, errors = validate_plugin_meta(make_meta(origin="alien"), "action")
        assert ok is False
        assert any("origin invalid: alien" in e for e in errors)

    def test_trigger_accepts_typed_outputs(self):
        meta = make_meta(
            outputs=[{"name": "path", "type": "string", "label": "文件路径"}]
        )
        ok, errors = validate_plugin_meta(meta, "trigger")
        assert ok is True
        assert errors == []

    def test_outputs_reject_invalid_type(self):
        meta = make_meta(
            outputs=[{"name": "x", "type": "weird", "label": "l"}]
        )
        ok, errors = validate_plugin_meta(meta, "trigger")
        assert ok is False
        assert any("outputs[0].type 无效" in e for e in errors)

    def test_outputs_reject_duplicate_names(self):
        meta = make_meta(
            outputs=[
                {"name": "x", "type": "string", "label": "l"},
                {"name": "x", "type": "number", "label": "l2"},
            ]
        )
        ok, errors = validate_plugin_meta(meta, "trigger")
        assert ok is False
        assert any("重复名称: x" in e for e in errors)

    def test_summary_policy_is_validated_for_params_and_outputs(self):
        meta = make_meta(
            params=[{
                "name": "token",
                "type": "string",
                "label": "令牌",
                "sensitive": True,
                "summary": "hidden",
            }],
            outputs=[{
                "name": "count",
                "type": "number",
                "label": "数量",
                "summary": "value",
            }],
        )

        ok, errors = validate_plugin_meta(meta, "action")

        assert ok is True
        assert errors == []

    def test_invalid_summary_policy_and_sensitive_type_are_rejected(self):
        meta = make_meta(
            params=[{
                "name": "token",
                "type": "string",
                "label": "令牌",
                "sensitive": "yes",
                "summary": "raw",
            }],
            outputs=[{
                "name": "result",
                "type": "string",
                "label": "结果",
                "summary": "raw",
            }],
        )

        ok, errors = validate_plugin_meta(meta, "action")

        assert ok is False
        assert any("params[0].sensitive 必须为布尔值" in error for error in errors)
        assert any("params[0].summary 无效" in error for error in errors)
        assert any("outputs[0].summary 无效" in error for error in errors)


class TestComponentsField:
    def test_valid_components_declaration_passes(self):
        meta = make_meta(components=[{
            "id": "record",
            "name": "录制",
            "description": "采集数据",
            "entrypoint": "component.py",
            "api": "component-v1",
            "ui": {
                "button_label": "录制",
                "icon": "keyboard",
                "description": "点一下开始采集",
            },
        }])
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is True, errors

    def test_components_requires_nonempty_list(self):
        for bad in ("yes", [], [{"id": "x"}]):
            ok, errors = validate_plugin_meta(make_meta(components=bad), "action")
            assert ok is False, bad

    def test_entrypoint_must_stay_inside_plugin_dir(self):
        for bad in ("../escape.py", "/absolute.py", "sub\\component.py"):
            ok, errors = validate_plugin_meta(
                make_meta(components=[{
                    "id": "record",
                    "name": "录制",
                    "entrypoint": bad,
                }]),
                "action",
            )
            assert ok is False, bad
            assert any("插件目录内" in error for error in errors), bad

    def test_duplicate_component_id_rejected(self):
        ok, errors = validate_plugin_meta(
            make_meta(components=[
                {"id": "record", "name": "录制", "entrypoint": "component.py"},
                {"id": "record", "name": "录制", "entrypoint": "component.py"},
            ]),
            "action",
        )
        assert ok is False
        assert any("重复 id" in error for error in errors)

    def test_ui_is_validated(self):
        ok, errors = validate_plugin_meta(
            make_meta(components=[{
                "id": "record",
                "name": "录制",
                "entrypoint": "component.py",
                "ui": {"bogus": "x", "button_label": 1},
            }]),
            "action",
        )
        assert ok is False
        assert any("components[0].ui 包含未知字段" in error for error in errors)
        assert any("components[0].ui.button_label 必须是字符串" in error for error in errors)

    def test_param_types_are_validated(self):
        meta = make_meta(components=[{
            "id": "record",
            "name": "录制",
            "entrypoint": "component.py",
            "param_types": ["hotkey", "bogus"],
        }])
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert any("param_types 包含无效参数类型" in error for error in errors)

    def test_macro_param_and_capture_only_are_supported(self):
        meta = make_meta(params=[{
            "name": "macro",
            "label": "操作宏",
            "type": "macro",
            "capture_only": True,
        }])
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is True, errors

    def test_capture_only_must_be_boolean(self):
        meta = make_meta(params=[{
            "name": "macro",
            "label": "操作宏",
            "type": "macro",
            "capture_only": "yes",
        }])
        ok, errors = validate_plugin_meta(meta, "action")
        assert ok is False
        assert any("capture_only 必须为布尔值" in error for error in errors)

    def test_unknown_component_field_rejected(self):
        ok, errors = validate_plugin_meta(
            make_meta(components=[{
                "id": "record",
                "name": "录制",
                "entrypoint": "component.py",
                "api": "recording-v1",
            }]),
            "action",
        )
        assert ok is False
        assert any("components[0].api 目前仅支持" in error for error in errors)

    def test_trigger_allows_components(self):
        meta = make_meta(components=[{
            "id": "record",
            "name": "录制热键",
            "entrypoint": "component.py",
        }])
        ok, errors = validate_plugin_meta(meta, "trigger")
        assert ok is True, errors
