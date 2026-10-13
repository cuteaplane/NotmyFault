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
    "workflow 是格式 2.1 的平面图，nodes 保存所有图块，parent 指定所属事件组或动作组。"
    "trigger 图块包含插件配置 trigger，occurrence 指定事件发生或未发生。"
    "edges 的 channel 为 condition 或 control，source、port、target、target_port 指定端口。"
    "event_group 的 group_type 为 if、all、any、not，IF 各分支 id 同时用作条件入口和执行出口。"
    "IF、多个 ELIF 按顺序判断，唯一 ELSE 不接条件；同一 IF 或 ELIF 的多个条件须先连接 AND 或 OR。"
    "action_group 从 entry 端口连接组内起始动作，从 next 端口继续外部动作，exports 提供内部结果。"
    "无连线的图块不参与执行，不自动生成条件、分支动作或连接。"
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
            "workflow": workflow,
            "constants": {"type": "array", "items": {"type": "object"}},
            "variables": {"type": "array", "items": {"type": "object"}},
            "concurrency": {"type": "object"},
        },
        "required": ["name", "workflow"],
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
