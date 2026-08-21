import json

import pytest

from notmyfault.host import ai_tools
from notmyfault.host.ai_tools import (
    DEFAULT_SKILL,
    PLUGIN_SOURCE_TOOL,
    RULE_DRAFT_TOOL,
    SkillSpec,
    ToolCall,
    ToolCallError,
    ToolSpec,
    chat_tool_definitions,
    extract_chat_text,
    extract_chat_tool_call,
    extract_responses_text,
    extract_responses_tool_call,
    responses_tool_definitions,
)


def rule_args(**overrides):
    args = {
        "name": "晚间提醒",
        "event": {"type": "time_schedule", "params": {"time": "20:00"}},
        "actions": [{"type": "notify", "params": {"message": "检查日报"}}],
    }
    args.update(overrides)
    return args


def chat_body(tool_calls):
    return {"choices": [{"message": {"tool_calls": tool_calls}}]}


def chat_function_call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


def responses_body(output):
    return {"output": output}


def responses_function_call(name, arguments, call_id="call_1"):
    return {
        "type": "function_call",
        "name": name,
        "arguments": arguments,
        "call_id": call_id,
    }


class TestSkillAllowlist:
    def test_default_skill_names_the_skill_and_exact_tools(self):
        assert isinstance(DEFAULT_SKILL, SkillSpec)
        assert DEFAULT_SKILL.name
        assert DEFAULT_SKILL.tool_names() == {
            "propose_rule_draft",
            "propose_plugin",
        }

    def test_has_tool_rejects_unknown(self):
        assert DEFAULT_SKILL.has_tool("propose_rule_draft") is True
        assert DEFAULT_SKILL.has_tool("propose_plugin") is True
        assert DEFAULT_SKILL.has_tool("reply") is False
        assert DEFAULT_SKILL.has_tool("propose_plugin_source") is False
        assert DEFAULT_SKILL.has_tool("install_plugin") is False

    def test_skill_and_tool_are_frozen(self):
        with pytest.raises(AttributeError):
            setattr(DEFAULT_SKILL, "name", "别的名字")
        with pytest.raises(AttributeError):
            setattr(DEFAULT_SKILL, "tools", ())
        assert isinstance(DEFAULT_SKILL.tools, tuple)
        assert isinstance(RULE_DRAFT_TOOL, ToolSpec)
        with pytest.raises(AttributeError):
            setattr(RULE_DRAFT_TOOL, "name", "改名")


class TestToolDefinitions:
    def test_chat_definitions_nest_under_function(self):
        definitions = json.loads(json.dumps(chat_tool_definitions(DEFAULT_SKILL)))
        assert [d["type"] for d in definitions] == ["function", "function"]
        names = {d["function"]["name"] for d in definitions}
        assert names == DEFAULT_SKILL.tool_names()
        for d in definitions:
            assert "name" not in d
            assert d["function"]["parameters"]["type"] == "object"

    def test_responses_definitions_are_flat(self):
        definitions = json.loads(json.dumps(responses_tool_definitions(DEFAULT_SKILL)))
        assert [d["type"] for d in definitions] == ["function", "function"]
        names = {d["name"] for d in definitions}
        assert names == DEFAULT_SKILL.tool_names()
        for d in definitions:
            assert "function" not in d
            assert d["parameters"]["type"] == "object"

    def test_definitions_are_json_serializable(self):
        json.dumps(chat_tool_definitions(DEFAULT_SKILL))
        json.dumps(responses_tool_definitions(DEFAULT_SKILL))


class TestExtractChatToolCall:
    def test_extracts_exactly_one_call(self):
        arguments = json.dumps(rule_args())
        call = extract_chat_tool_call(
            chat_body([chat_function_call("propose_rule_draft", arguments)]),
            DEFAULT_SKILL,
        )
        assert call == ToolCall(
            name="propose_rule_draft",
            arguments=rule_args(),
            call_id=None,
        )

    def test_rejects_zero_calls(self):
        with pytest.raises(ToolCallError) as exc:
            extract_chat_tool_call(chat_body([]), DEFAULT_SKILL)
        assert exc.value.code == "no_tool_call"

    def test_rejects_multiple_calls(self):
        body = chat_body([
            chat_function_call("propose_rule_draft", "{}"),
            chat_function_call("propose_plugin", "{}"),
        ])
        with pytest.raises(ToolCallError) as exc:
            extract_chat_tool_call(body, DEFAULT_SKILL)
        assert exc.value.code == "multiple_tool_calls"

    def test_rejects_malformed_json_arguments(self):
        body = chat_body([
            chat_function_call("propose_rule_draft", "not-json"),
        ])
        with pytest.raises(ToolCallError) as exc:
            extract_chat_tool_call(body, DEFAULT_SKILL)
        assert exc.value.code == "bad_arguments"

    def test_rejects_non_object_arguments(self):
        body = chat_body([
            chat_function_call("propose_rule_draft", json.dumps([1, 2, 3])),
        ])
        with pytest.raises(ToolCallError) as exc:
            extract_chat_tool_call(body, DEFAULT_SKILL)
        assert exc.value.code == "bad_arguments"

    def test_rejects_unknown_tool_name(self):
        body = chat_body([chat_function_call("delete_everything", "{}")])
        with pytest.raises(ToolCallError) as exc:
            extract_chat_tool_call(body, DEFAULT_SKILL)
        assert exc.value.code == "unknown_tool"


class TestExtractResponsesToolCall:
    def test_extracts_exactly_one_call_with_call_id(self):
        arguments = json.dumps(rule_args())
        call = extract_responses_tool_call(
            responses_body([responses_function_call(
                "propose_rule_draft", arguments, call_id="call_abc",
            )]),
            DEFAULT_SKILL,
        )
        assert call == ToolCall(
            name="propose_rule_draft",
            arguments=rule_args(),
            call_id="call_abc",
        )

    def test_skips_non_function_output_items(self):
        output = [
            {"type": "message", "content": [{"type": "output_text", "text": "hi"}]},
            responses_function_call("propose_plugin", "{}", call_id="call_2"),
        ]
        call = extract_responses_tool_call(responses_body(output), DEFAULT_SKILL)
        assert call.name == "propose_plugin"
        assert call.call_id == "call_2"

    def test_rejects_zero_calls(self):
        with pytest.raises(ToolCallError) as exc:
            extract_responses_tool_call(responses_body([]), DEFAULT_SKILL)
        assert exc.value.code == "no_tool_call"

    def test_rejects_multiple_calls(self):
        output = [
            responses_function_call("propose_rule_draft", "{}", "a"),
            responses_function_call("propose_plugin", "{}", "b"),
        ]
        with pytest.raises(ToolCallError) as exc:
            extract_responses_tool_call(responses_body(output), DEFAULT_SKILL)
        assert exc.value.code == "multiple_tool_calls"

    def test_rejects_unknown_tool_name(self):
        output = [responses_function_call("run_code", "{}", "c")]
        with pytest.raises(ToolCallError) as exc:
            extract_responses_tool_call(responses_body(output), DEFAULT_SKILL)
        assert exc.value.code == "unknown_tool"

    def test_rejects_malformed_json_arguments(self):
        output = [responses_function_call("propose_rule_draft", "{{{{", "d")]
        with pytest.raises(ToolCallError) as exc:
            extract_responses_tool_call(responses_body(output), DEFAULT_SKILL)
        assert exc.value.code == "bad_arguments"


class TestStructuredReplyTool:
    def test_reply_tool_not_in_default_skill(self):
        assert DEFAULT_SKILL.has_tool("reply") is False


class TestPluginSourceTool:
    def test_source_tool_schema_is_strict(self):
        schema = json.loads(json.dumps(PLUGIN_SOURCE_TOOL.parameters))
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["kind", "manifest", "source"]
        assert schema["properties"]["kind"] == {
            "type": "string",
            "enum": ["trigger", "action"],
        }
        assert schema["properties"]["manifest"] == {"type": "object"}
        assert schema["properties"]["source"] == {"type": "string"}

    def test_source_tool_absent_from_default_skill(self):
        assert DEFAULT_SKILL.has_tool("propose_plugin_source") is False


class TestExtractAssistantText:
    def test_chat_text_from_string_content(self):
        body = {"choices": [{"message": {"content": "你好，我来帮你。"}}]}
        assert extract_chat_text(body) == "你好，我来帮你。"

    def test_chat_text_from_part_list_content(self):
        body = {"choices": [{"message": {"content": [
            {"type": "text", "text": "第一部分"},
            {"type": "text", "text": "第二部分"},
        ]}}]}
        assert extract_chat_text(body) == "第一部分第二部分"

    def test_chat_text_missing_choices_is_empty(self):
        assert extract_chat_text({}) == ""
        assert extract_chat_text({"choices": []}) == ""

    def test_chat_text_null_or_missing_content_is_empty(self):
        assert extract_chat_text({"choices": [{"message": {"content": None}}]}) == ""
        assert extract_chat_text({"choices": [{"message": {}}]}) == ""

    def test_chat_text_non_object_body_is_empty(self):
        assert extract_chat_text(["not", "an", "object"]) == ""
        assert extract_chat_text(None) == ""

    def test_responses_text_from_message_output(self):
        body = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "回复内容"},
        ]}]}
        assert extract_responses_text(body) == "回复内容"

    def test_responses_text_joins_multiple_outputs(self):
        body = {"output": [
            {"type": "message", "content": [{"type": "output_text", "text": "第一段"}]},
            {"type": "function_call", "name": "reply", "arguments": "{}"},
            {"type": "message", "content": [{"type": "output_text", "text": "第二段"}]},
        ]}
        assert extract_responses_text(body) == "第一段第二段"

    def test_responses_text_missing_output_is_empty(self):
        assert extract_responses_text({}) == ""
        assert extract_responses_text({"output": []}) == ""

    def test_responses_text_skips_refusal(self):
        body = {"output": [{"type": "message", "content": [
            {"type": "refusal", "refusal": "我不能这样做"},
        ]}]}
        assert extract_responses_text(body) == ""


class TestNoSideEffects:
    def test_module_exposes_no_execution_api(self):
        forbidden = {
            "install", "uninstall", "sign", "load", "execute", "exec", "run",
            "write", "save", "build", "compile", "subprocess", "importlib",
            "os", "pathlib", "open", "eval", "persist",
        }
        exposed = set(vars(ai_tools))
        assert not (forbidden & exposed)
