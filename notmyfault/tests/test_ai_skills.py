import json

import pytest

from notmyfault.host.ai_skills import build_rule_drafting_skill, plugin_authoring_guidance
from notmyfault.host.ai_tools import (
    SkillSpec,
    chat_tool_definitions,
    responses_tool_definitions,
)
from notmyfault.security.plugin_schema import PERMISSION_REGISTRY

_PARAM_TYPES = [
    "bool", "hotkey", "macro", "number", "path", "plugin_data",
    "select", "string", "textarea", "time", "uia_selector",
]
_OUTPUT_TYPES = ["any", "array", "bool", "number", "object", "string"]


def make_catalog():
    return {
        "triggers": {
            "time_schedule": {"name": "定时", "params": []},
            "usb_insert": {"name": "U 盘插入", "params": []},
        },
        "actions": {
            "notify": {"name": "显示通知", "params": []},
            "open_url": {"name": "打开网址", "params": []},
        },
    }


def tool_schema(catalog, name):
    skill = build_rule_drafting_skill(catalog)
    tool = next(t for t in skill.tools if t.name == name)
    return json.loads(json.dumps(tool.parameters))


class TestBuildRuleDraftingSkill:
    def test_returns_skill_with_rule_plugin_and_reply_tools(self):
        skill = build_rule_drafting_skill(make_catalog())
        assert isinstance(skill, SkillSpec)
        assert skill.tool_names() == {"propose_rule_draft", "propose_plugin", "reply"}

    def test_rule_event_enum_is_sorted_trigger_ids(self):
        schema = tool_schema(make_catalog(), "propose_rule_draft")
        event = schema["properties"]["event"]
        assert event["properties"]["type"]["enum"] == ["time_schedule", "usb_insert"]

    def test_rule_action_enum_is_sorted_action_ids(self):
        schema = tool_schema(make_catalog(), "propose_rule_draft")
        item = schema["properties"]["actions"]["items"]
        assert item["properties"]["type"]["enum"] == ["notify", "open_url"]

    def test_rule_schema_keeps_additional_properties_false(self):
        schema = tool_schema(make_catalog(), "propose_rule_draft")
        assert schema["additionalProperties"] is False
        event = schema["properties"]["event"]
        assert event["additionalProperties"] is False
        assert event["properties"]["params"]["type"] == "object"
        item = schema["properties"]["actions"]["items"]
        assert item["additionalProperties"] is False

    def test_enums_track_catalog(self):
        other = {
            "triggers": {"power_state": {}, "idle_detect": {}},
            "actions": {"shutdown": {}},
        }
        schema = tool_schema(other, "propose_rule_draft")
        assert schema["properties"]["event"]["properties"]["type"]["enum"] == [
            "idle_detect", "power_state",
        ]
        assert schema["properties"]["actions"]["items"]["properties"]["type"]["enum"] == [
            "shutdown",
        ]


class TestProposalSchema:
    def test_parameter_item_schema_is_strict(self):
        schema = tool_schema(make_catalog(), "propose_plugin")
        parameters = schema["properties"]["parameters"]
        assert parameters["type"] == "array"
        item = parameters["items"]
        assert item["type"] == "object"
        assert item["required"] == ["name", "type", "label"]
        assert item["additionalProperties"] is False
        assert set(item["properties"]) == {
            "name", "type", "label", "default", "placeholder", "rows",
            "options", "min", "max", "step", "visible_when", "value_type",
            "data_type", "required", "sensitive", "capture_only", "summary",
        }
        assert item["properties"]["type"]["enum"] == _PARAM_TYPES

    def test_output_item_schema_is_strict(self):
        schema = tool_schema(make_catalog(), "propose_plugin")
        item = schema["properties"]["outputs"]["items"]
        assert item["required"] == ["name", "type", "label"]
        assert item["additionalProperties"] is False
        assert item["properties"]["type"]["enum"] == _OUTPUT_TYPES

    def test_permission_enum_is_sorted_registry(self):
        schema = tool_schema(make_catalog(), "propose_plugin")
        permissions = schema["properties"]["permissions"]["items"]
        assert permissions["type"] == "string"
        assert permissions["enum"] == sorted(PERMISSION_REGISTRY)


class TestPluginSourceGating:
    def test_source_tool_absent_by_default(self):
        skill = build_rule_drafting_skill(make_catalog())
        assert skill.has_tool("propose_plugin_source") is False

    def test_source_tool_present_with_consent(self):
        skill = build_rule_drafting_skill(make_catalog(), allow_plugin_source=True)
        assert skill.has_tool("propose_plugin_source") is True

    def test_consent_keeps_existing_tools(self):
        skill = build_rule_drafting_skill(make_catalog(), allow_plugin_source=True)
        assert {"propose_rule_draft", "propose_plugin", "reply"} <= skill.tool_names()

    def test_source_tool_schema_is_strict(self):
        skill = build_rule_drafting_skill(make_catalog(), allow_plugin_source=True)
        tool = next(t for t in skill.tools if t.name == "propose_plugin_source")
        schema = json.loads(json.dumps(tool.parameters))
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["kind", "manifest", "source"]
        assert schema["properties"]["kind"]["enum"] == ["trigger", "action"]
        assert schema["properties"]["manifest"] == {"type": "object"}
        assert schema["properties"]["source"] == {"type": "string"}


class TestPluginAuthoringGuidance:
    def test_guidance_is_deterministic(self):
        assert plugin_authoring_guidance() == plugin_authoring_guidance()

    def test_guidance_mentions_manifest_essentials(self):
        text = plugin_authoring_guidance()
        for field in ("id", "name", "description", "enabled",
                      "version_code", "version", "package_name"):
            assert field in text
        assert "semantic" in text
        assert "trigger_api" in text
        assert "execution_api" in text

    def test_guidance_mentions_entry_signatures(self):
        text = plugin_authoring_guidance()
        assert "run(meta, config, emit_event, shutdown_event)" in text
        assert "run(action_info, params)" in text
        assert "run_with_context(action_info, params, context)" in text

    def test_guidance_lists_types_and_permissions(self):
        text = plugin_authoring_guidance()
        for ptype in _PARAM_TYPES:
            assert ptype in text
        for otype in _OUTPUT_TYPES:
            assert otype in text
        for perm in ("notification", "network", "filesystem"):
            assert perm in text

    def test_guidance_forbids_hardcoded_targets(self):
        text = plugin_authoring_guidance()
        assert "QQ" in text
        assert "窗口标题" in text
        assert "用户路径" in text
        assert "参数" in text

    def test_guidance_is_generalized_and_review_only(self):
        text = plugin_authoring_guidance()
        assert "一类任务" in text
        assert "审查" in text
        assert "不自动" in text


class TestDefinitionsNesting:
    def test_chat_and_responses_keep_distinct_nesting(self):
        skill = build_rule_drafting_skill(make_catalog())
        chat = json.loads(json.dumps(chat_tool_definitions(skill)))
        responses = json.loads(json.dumps(responses_tool_definitions(skill)))
        for definition in chat:
            assert "function" in definition
            assert "name" not in definition
        for definition in responses:
            assert "name" in definition
            assert "function" not in definition
