"""按插件目录构造规则草稿工具 schema。"""

from __future__ import annotations

from notmyfault.host.ai_rule_draft import PluginCatalog
from notmyfault.host.ai_tools import JSON, SkillSpec, ToolSpec

_SKILL_DESCRIPTION = (
    "目录中的触发器和动作能够满足需求时使用规则草稿工具；"
    "需要说明缺少的能力、澄清或追问时直接回复普通文字。"
)
_RULE_DRAFT_DESCRIPTION = (
    "目录中的触发器和动作能够满足需求时，把需求写成规则候选。"
)


def _sorted_ids(catalog: PluginCatalog, key: str) -> list[str]:
    section = catalog.get(key)
    if not isinstance(section, dict):
        return []
    return sorted(section)


def _node_schema(type_ids: list[str]) -> JSON:
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": type_ids},
            "params": {"type": "object"},
        },
        "required": ["type", "params"],
        "additionalProperties": False,
    }


def _rule_draft_parameters(catalog: PluginCatalog) -> JSON:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "event": _node_schema(_sorted_ids(catalog, "triggers")),
            "preconditions": {
                "type": "array",
                "description": "执行前检查，全部通过才继续动作；仅在用户明确提出条件时填写。",
                "items": _node_schema(_sorted_ids(catalog, "actions")),
            },
            "actions": {
                "type": "array",
                "items": _node_schema(_sorted_ids(catalog, "actions")),
            },
        },
        "required": ["name", "event", "actions"],
        "additionalProperties": False,
    }


def build_rule_drafting_skill(catalog: PluginCatalog) -> SkillSpec:
    return SkillSpec(
        name="rule_drafting",
        description=_SKILL_DESCRIPTION,
        tools=(
            ToolSpec(
                name="propose_rule_draft",
                description=_RULE_DRAFT_DESCRIPTION,
                parameters=_rule_draft_parameters(catalog),
            ),
        ),
    )
