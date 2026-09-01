"""插件元数据 Schema。"""

from notmyfault.security.plugin_schema import validate_plugin_meta


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

    def test_unsafe_parameter_names_are_rejected(self):
        for name in ("__proto__", "constructor", "prototype", "space key"):
            ok, errors = validate_plugin_meta(
                make_meta(params=[{"name": name, "type": "string", "label": "值"}]),
                "action",
            )
            assert ok is False
            assert "params[0].name 不是安全字段名" in errors

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

    def test_isolated_action_rejects_runtime_cancellation(self):
        ok, errors = validate_plugin_meta(
            make_meta(
                execution_mode="isolated",
                execution_api="context-v1",
                cancellation_api="runtime-v1",
            ),
            "action",
        )
        assert ok is False
        assert "isolated 动作暂不支持 cancellation_api" in errors

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

    def test_isolated_mode_is_valid_for_every_origin(self):
        for origin in ("builtin", "user", "third_party"):
            ok, errors = validate_plugin_meta(
                make_meta(origin=origin, execution_mode="isolated"), "action"
            )
            assert ok is True, errors

            ok, errors = validate_plugin_meta(
                make_meta(
                    origin=origin,
                    execution_mode="isolated",
                    permissions=["admin"],
                    security={"admin_executables": ["cmd.exe"]},
                ),
                "action",
            )
            assert ok is False
            assert "isolated 动作暂不支持 admin 权限" in errors

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


class TestMacroParams:
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
