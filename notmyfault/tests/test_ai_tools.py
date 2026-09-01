"""AI 规则工具在 Chat Completions 与 Responses 格式中的公开行为。"""

import json

import pytest

from notmyfault.host.ai_tools import (
    DEFAULT_SKILL,
    ToolCall,
    ToolCallError,
    extract_chat_text,
    extract_chat_tool_call,
    extract_responses_text,
    extract_responses_tool_call,
)


RULE_ARGS = {
    "name": "晚间提醒",
    "event": {"type": "time_schedule", "params": {"time": "20:00"}},
    "actions": [{"type": "notify", "params": {"message": "检查日报"}}],
}


def chat_call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


def response_call(name, arguments, call_id="call_1"):
    return {
        "type": "function_call",
        "name": name,
        "arguments": arguments,
        "call_id": call_id,
    }


def test_default_skill_only_allows_rule_drafts():
    assert DEFAULT_SKILL.tool_names() == {"propose_rule_draft"}
    for removed in ("propose_plugin", "propose_plugin_source", "install_plugin", "reply"):
        assert DEFAULT_SKILL.has_tool(removed) is False


def test_both_provider_formats_extract_one_rule_call():
    arguments = json.dumps(RULE_ARGS)
    chat = extract_chat_tool_call(
        {"choices": [{"message": {"tool_calls": [chat_call("propose_rule_draft", arguments)]}}]},
        DEFAULT_SKILL,
    )
    responses = extract_responses_tool_call(
        {"output": [response_call("propose_rule_draft", arguments, "call_rule")]},
        DEFAULT_SKILL,
    )
    assert chat == ToolCall("propose_rule_draft", RULE_ARGS, None)
    assert responses == ToolCall("propose_rule_draft", RULE_ARGS, "call_rule")


@pytest.mark.parametrize(
    ("body", "extractor", "code"),
    [
        ({"choices": [{"message": {"tool_calls": []}}]}, extract_chat_tool_call, "no_tool_call"),
        ({"output": []}, extract_responses_tool_call, "no_tool_call"),
        (
            {"choices": [{"message": {"tool_calls": [chat_call("run_code", "{}")]}}]},
            extract_chat_tool_call,
            "unknown_tool",
        ),
        (
            {"output": [response_call("propose_rule_draft", "not-json")]},
            extract_responses_tool_call,
            "bad_arguments",
        ),
        (
            {"output": [response_call("propose_rule_draft", "{}", "a"), response_call("propose_rule_draft", "{}", "b")]},
            extract_responses_tool_call,
            "multiple_tool_calls",
        ),
    ],
)
def test_tool_call_errors_use_stable_codes(body, extractor, code):
    with pytest.raises(ToolCallError) as caught:
        extractor(body, DEFAULT_SKILL)
    assert caught.value.code == code


def test_assistant_text_is_extracted_from_both_provider_formats():
    chat = {"choices": [{"message": {"content": [
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
    ]}}]}
    responses = {"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "第一段"}]},
        response_call("propose_rule_draft", "{}"),
        {"type": "message", "content": [{"type": "output_text", "text": "第二段"}]},
    ]}
    assert extract_chat_text(chat) == "第一段第二段"
    assert extract_responses_text(responses) == "第一段第二段"
    assert extract_chat_text({}) == ""
    assert extract_responses_text({}) == ""
