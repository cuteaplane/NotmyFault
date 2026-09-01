"""AI 技能与工具契约。

技能列出模型可调的工具白名单，工具定义按 Chat/Responses 两种接口生成，
工具调用从模型返回里抽出唯一一次。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence, TypeAlias

JSON: TypeAlias = Mapping[str, "JSON"] | Sequence["JSON"] | str | int | float | bool | None


class ToolCallError(ValueError):
    """工具调用解析或参数校验失败，code 稳定可断言。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: JSON


@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    description: str
    tools: tuple[ToolSpec, ...]

    def tool_names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self.tools)

    def has_tool(self, name: str) -> bool:
        return name in self.tool_names()


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: Mapping[str, JSON]
    call_id: str | None = None


_NODE_SCHEMA: JSON = {
    "type": "object",
    "properties": {"type": {"type": "string"}, "params": {"type": "object"}},
    "required": ["type", "params"],
    "additionalProperties": False,
}

_RULE_DRAFT_PARAMETERS: JSON = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "event": _NODE_SCHEMA,
        "actions": {"type": "array", "items": _NODE_SCHEMA},
    },
    "required": ["name", "event", "actions"],
    "additionalProperties": False,
}

RULE_DRAFT_TOOL = ToolSpec(
    name="propose_rule_draft",
    description="把规则描述转成只读的规则候选，不保存也不执行。",
    parameters=_RULE_DRAFT_PARAMETERS,
)
DEFAULT_SKILL = SkillSpec(
    name="rule_drafting",
    description="规则草稿工具；说明缺少的能力、澄清和追问使用普通文本。",
    tools=(RULE_DRAFT_TOOL,),
)


def chat_tool_definitions(skill: SkillSpec) -> list[JSON]:
    definitions: list[JSON] = []
    for tool in skill.tools:
        definitions.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        })
    return definitions


def responses_tool_definitions(skill: SkillSpec) -> list[JSON]:
    definitions: list[JSON] = []
    for tool in skill.tools:
        definitions.append({
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        })
    return definitions


def _require_str(value: JSON, location: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolCallError(code, f"{location} 必须是非空字符串")
    return value


def _require_object(value: JSON, location: str, code: str) -> dict[str, JSON]:
    if not isinstance(value, dict):
        raise ToolCallError(code, f"{location} 必须是对象")
    return value


def _require_list(
    value: JSON, location: str, code: str, *, nonempty: bool = False
) -> list[JSON]:
    if not isinstance(value, list) or (nonempty and not value):
        kind = "非空数组" if nonempty else "数组"
        raise ToolCallError(code, f"{location} 必须是{kind}")
    return value


def _parse_arguments_json(raw: JSON) -> Mapping[str, JSON]:
    if not isinstance(raw, str):
        raise ToolCallError("bad_arguments", "工具参数必须是 JSON 字符串")
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise ToolCallError("bad_arguments", "工具参数不是有效 JSON") from error
    if not isinstance(parsed, dict):
        raise ToolCallError("bad_arguments", "工具参数必须是 JSON 对象")
    return parsed


def _make_tool_call(
    name: str, raw_arguments: JSON, call_id: str | None, skill: SkillSpec
) -> ToolCall:
    if not skill.has_tool(name):
        raise ToolCallError("unknown_tool", f"技能 {skill.name} 不允许工具 {name}")
    return ToolCall(
        name=name, arguments=_parse_arguments_json(raw_arguments), call_id=call_id
    )


def extract_chat_tool_call(body: JSON, skill: SkillSpec) -> ToolCall:
    body = _require_object(body, "Chat 返回", "bad_arguments")
    choices = _require_list(body.get("choices"), "Chat 返回 choices", "no_tool_call", nonempty=True)
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    tool_calls = _require_list(
        message.get("tool_calls") if isinstance(message, dict) else None,
        "Chat 返回 tool_calls",
        "no_tool_call",
        nonempty=True,
    )
    if len(tool_calls) != 1:
        raise ToolCallError("multiple_tool_calls", "Chat 返回了多个工具调用")
    call = tool_calls[0]
    function = _require_object(
        call.get("function") if isinstance(call, dict) else None,
        "Chat 工具调用 function",
        "bad_arguments",
    )
    name = _require_str(function.get("name"), "Chat 工具调用 name", "bad_arguments")
    return _make_tool_call(name, function.get("arguments"), None, skill)


def extract_responses_tool_call(body: JSON, skill: SkillSpec) -> ToolCall:
    body = _require_object(body, "Responses 返回", "bad_arguments")
    output = _require_list(body.get("output"), "Responses 返回 output", "no_tool_call")
    calls = [
        item for item in output
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]
    if not calls:
        raise ToolCallError("no_tool_call", "Responses 返回没有工具调用")
    if len(calls) != 1:
        raise ToolCallError("multiple_tool_calls", "Responses 返回了多个工具调用")
    call = _require_object(calls[0], "Responses 工具调用", "bad_arguments")
    name = _require_str(call.get("name"), "Responses 工具调用 name", "bad_arguments")
    call_id = call.get("call_id")
    return _make_tool_call(
        name,
        call.get("arguments"),
        call_id if isinstance(call_id, str) else None,
        skill,
    )


def _text_from_content(content: JSON) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def extract_chat_text(body: JSON) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return _text_from_content(content)


def extract_responses_text(body: JSON) -> str:
    if not isinstance(body, dict):
        return ""
    output = body.get("output")
    if not isinstance(output, list):
        return _text_from_content(body.get("output_text"))
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if part.get("type") in ("output_text", "text") and isinstance(text, str):
                    parts.append(text)
    return "".join(parts)
