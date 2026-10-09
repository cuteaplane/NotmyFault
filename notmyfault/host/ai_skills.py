"""按插件目录构造规则草稿工具 schema。"""

from __future__ import annotations

from notmyfault.host.ai_rule_draft import PluginCatalog
from notmyfault.host.ai_tools import JSON, SkillSpec, ToolSpec
from notmyfault.host.ai_graph_schema import workflow_draft_schema

_SKILL_DESCRIPTION = (
    "目录中的触发器和动作能够满足需求时使用规则草稿工具；"
    "需要说明缺少的能力、澄清或追问时直接回复普通文字。"
)
_RULE_DRAFT_DESCRIPTION = (
    "目录中的触发器和动作能够满足需求时，把需求写成规则候选，不保存或执行。"
    "使用 workflow 保存控制图，entry 指向入口节点，edges 按 source、port、target 决定执行顺序。"
    "入口 event_group 的 activation 配置启动事件组合；中途组的 mode 为 immediate 或 wait。"
    "branches 按 IF、ELIF 顺序判断，最后提供 else 出口，port 使用分支 id。"
    "condition_graph 的边从子条件指向 all、any、not 节点，order 指定短路顺序。"
    "action_group.workflow 保存内部图，组外通过 exports 读取内部结果。"
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
    workflow, definitions = workflow_draft_schema(_sorted_ids(catalog, "actions"), _sorted_ids(catalog, "triggers"))
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "condition": _node_schema(_sorted_ids(catalog, "triggers")),
            "actions": {
                "type": "array",
                "items": _node_schema(_sorted_ids(catalog, "actions")),
            },
            "workflow": workflow,
            "constants": {"type": "array", "items": {"type": "object"}},
            "variables": {"type": "array", "items": {"type": "object"}},
            "concurrency": {"type": "object"},
        },
        "required": ["name"],
        "anyOf": [{"required": ["condition", "actions"]}, {"required": ["workflow"]}],
        "$defs": definitions,
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
