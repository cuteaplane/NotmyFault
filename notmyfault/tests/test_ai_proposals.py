import json

import pytest

from notmyfault.host import ai_proposals
from notmyfault.host.ai_proposals import (
    parse_plugin_proposal,
    parse_rule_draft,
)
from notmyfault.host.ai_tools import ToolCallError


def make_catalog():
    return {
        "triggers": {
            "time_schedule": {
                "name": "定时",
                "params": [
                    {"name": "time", "type": "time", "label": "时间"},
                    {"name": "repeat", "type": "select", "label": "重复"},
                ],
            },
            "usb_insert": {"name": "U 盘插入", "params": []},
        },
        "actions": {
            "notify": {
                "name": "显示通知",
                "params": [
                    {"name": "title", "type": "string", "label": "标题"},
                    {"name": "message", "type": "string", "label": "内容"},
                ],
            },
            "open_url": {
                "name": "打开网址",
                "params": [{"name": "url", "type": "string", "label": "网址"}],
            },
        },
    }


def rule_args(**overrides):
    args = {
        "name": "晚间提醒",
        "event": {"type": "time_schedule", "params": {"time": "20:00"}},
        "actions": [{"type": "notify", "params": {"message": "检查日报"}}],
    }
    args.update(overrides)
    return args


class TestParseRuleDraft:
    def test_valid_rule_returns_normalized_proposal(self):
        result = parse_rule_draft(rule_args(), make_catalog())
        assert result == {
            "name": "晚间提醒",
            "event": {"type": "time_schedule", "params": {"time": "20:00"}},
            "actions": [{"type": "notify", "params": {"message": "检查日报"}}],
        }

    def test_preserves_multiple_actions_and_param_values(self):
        args = {
            "name": "多重动作",
            "event": {"type": "time_schedule", "params": {"time": "09:00", "repeat": "daily"}},
            "actions": [
                {"type": "notify", "params": {"title": "提醒", "message": "开会"}},
                {"type": "open_url", "params": {"url": "https://example.com"}},
            ],
        }
        result = parse_rule_draft(args, make_catalog())
        assert result == {
            "name": "多重动作",
            "event": {"type": "time_schedule", "params": {"time": "09:00", "repeat": "daily"}},
            "actions": [
                {"type": "notify", "params": {"title": "提醒", "message": "开会"}},
                {"type": "open_url", "params": {"url": "https://example.com"}},
            ],
        }

    @pytest.mark.parametrize("bad_args", [
        {"name": "x", "event": {"type": "time_schedule", "params": {}},
         "actions": [], "extra": 1},
        {"event": {"type": "time_schedule", "params": {}}, "actions": []},
        {"name": "", "event": {"type": "time_schedule", "params": {}}, "actions": []},
        {"name": "x", "actions": []},
        {"name": "x", "event": "not-an-object", "actions": []},
        {"name": "x", "event": {"type": "time_schedule", "params": {}}, "actions": "nope"},
        {"name": "x", "event": {"type": "time_schedule", "params": {}}, "actions": [123]},
    ])
    def test_rejects_malformed_rule_shape(self, bad_args):
        with pytest.raises(ToolCallError) as exc:
            parse_rule_draft(bad_args, make_catalog())
        assert exc.value.code == "rule_invalid"

    def test_rejects_extra_node_field(self):
        args = rule_args(event={"type": "time_schedule", "params": {}, "code": "x"})
        with pytest.raises(ToolCallError) as exc:
            parse_rule_draft(args, make_catalog())
        assert exc.value.code == "rule_invalid"

    def test_rejects_unknown_trigger_id(self):
        args = rule_args(event={"type": "ghost_trigger", "params": {}})
        with pytest.raises(ToolCallError) as exc:
            parse_rule_draft(args, make_catalog())
        assert exc.value.code == "rule_invalid"

    def test_rejects_unknown_action_id(self):
        args = rule_args(actions=[{"type": "ghost_action", "params": {}}])
        with pytest.raises(ToolCallError) as exc:
            parse_rule_draft(args, make_catalog())
        assert exc.value.code == "rule_invalid"

    def test_rejects_cross_plugin_param_on_trigger(self):
        args = rule_args(event={"type": "usb_insert", "params": {"time": "09:00"}})
        with pytest.raises(ToolCallError) as exc:
            parse_rule_draft(args, make_catalog())
        assert exc.value.code == "rule_invalid"

    def test_rejects_cross_plugin_param_on_action(self):
        args = rule_args(actions=[{"type": "notify", "params": {"url": "https://x"}}])
        with pytest.raises(ToolCallError) as exc:
            parse_rule_draft(args, make_catalog())
        assert exc.value.code == "rule_invalid"


class TestParsePluginProposal:
    def test_required_fields_only(self):
        result = parse_plugin_proposal({
            "kind": "trigger",
            "id": "motion_detect",
            "name": "运动检测",
            "description": "摄像头前有人经过时触发",
        })
        assert result == {
            "kind": "trigger",
            "id": "motion_detect",
            "name": "运动检测",
            "description": "摄像头前有人经过时触发",
        }

    def test_optional_fields_are_kept(self):
        result = parse_plugin_proposal({
            "kind": "action",
            "id": "post_webhook",
            "name": "发 webhook",
            "description": "向给定地址发 POST",
            "parameters": [{
                "name": "url", "type": "string", "label": "网址",
                "default": "https://example.com",
                "placeholder": "https://example.com/hook",
            }],
            "outputs": [{"name": "status", "type": "number", "label": "状态码"}],
            "permissions": ["network"],
            "rationale": "当前目录没有网络动作",
            "acceptance_criteria": ["能发 POST 并回传状态码"],
        })
        assert result["kind"] == "action"
        assert result["permissions"] == ["network"]
        assert result["parameters"] == [{
            "name": "url", "type": "string", "label": "网址",
            "default": "https://example.com",
            "placeholder": "https://example.com/hook",
        }]
        assert result["outputs"] == [{"name": "status", "type": "number", "label": "状态码"}]
        assert result["acceptance_criteria"] == ["能发 POST 并回传状态码"]

    def test_parameter_metadata_is_kept(self):
        parameters = [
            {
                "name": "mode", "type": "select", "label": "模式",
                "default": "fast", "options": ["fast", "safe"],
                "required": True,
            },
            {
                "name": "retries", "type": "number", "label": "重试次数",
                "default": 1, "min": 0, "max": 5, "step": 1,
                "visible_when": {"mode": ["safe"]},
            },
            {
                "name": "notes", "type": "textarea", "label": "备注",
                "placeholder": "可选说明", "rows": 4,
            },
            {
                "name": "secret", "type": "plugin_data", "label": "私有数据",
                "data_type": "secret_data", "value_type": "object",
                "sensitive": True, "capture_only": True, "summary": "hidden",
            },
        ]
        result = parse_plugin_proposal({
            "kind": "action",
            "id": "metadata_demo",
            "name": "参数元数据演示",
            "description": "保留插件参数编辑所需的声明信息",
            "parameters": parameters,
        })
        assert result["parameters"] == parameters

    @pytest.mark.parametrize("field", ["kind", "id", "name", "description"])
    def test_rejects_missing_required_field(self, field):
        args = {
            "kind": "action",
            "id": "x",
            "name": "x",
            "description": "x",
        }
        del args[field]
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    def test_rejects_unknown_kind(self):
        args = {"kind": "widget", "id": "x", "name": "x", "description": "x"}
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    def test_rejects_unknown_permission(self):
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "permissions": ["network", "teleport"],
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    @pytest.mark.parametrize("field", [
        "source", "code", "archive", "url", "build", "entrypoint",
        "components", "handler", "signature", "install_path",
    ])
    def test_rejects_install_source_fields(self, field):
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            field: "evil",
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"

    def test_rejects_arbitrary_extra_field(self):
        args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "exploit_hook": True,
        }
        with pytest.raises(ToolCallError) as exc:
            parse_plugin_proposal(args)
        assert exc.value.code == "proposal_invalid"


class TestNoSideEffects:
    def test_module_exposes_no_execution_api(self):
        forbidden = {
            "install", "uninstall", "sign", "load", "execute", "exec", "run",
            "write", "save", "build", "compile", "subprocess", "importlib",
            "os", "pathlib", "open", "eval", "persist",
        }
        exposed = set(vars(ai_proposals))
        assert not (forbidden & exposed)

    def test_parse_returns_data_only_and_does_not_mutate_input(self):
        args = rule_args()
        snapshot = json.loads(json.dumps(args))
        result = parse_rule_draft(args, make_catalog())
        assert args == snapshot
        assert result is not args
        json.dumps(result)

        proposal_args = {
            "kind": "action", "id": "x", "name": "x", "description": "x",
            "permissions": ["network"],
        }
        proposal_snapshot = json.loads(json.dumps(proposal_args))
        proposal = parse_plugin_proposal(proposal_args)
        assert proposal_args == proposal_snapshot
        assert proposal is not proposal_args
        json.dumps(proposal)
