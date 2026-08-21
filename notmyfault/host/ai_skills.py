"""按插件目录构造工具 schema。

build_rule_drafting_skill 把目录里的触发器/动作 id 填进规则工具的 enum，
把参数/输出类型和权限名填进提案工具的 enum。
"""

from __future__ import annotations

from notmyfault.host.ai_proposals import PluginCatalog, _OUTPUT_TYPES, _PARAM_TYPES
from notmyfault.host.ai_tools import (
    JSON,
    PLUGIN_SOURCE_TOOL,
    SkillSpec,
    ToolSpec,
)
from notmyfault.security.plugin_schema import PERMISSION_REGISTRY

_SKILL_DESCRIPTION = (
    "先看目录里有没有能满足需求的触发器和动作：有就用规则草稿工具，缺了就用提案工具；"
    "需要文字说明、澄清或追问时直接用普通文字回复，不要调工具，这样用户能看到流式输出。"
)
_RULE_DRAFT_DESCRIPTION = (
    "目录里已有能满足需求的触发器和动作时，选这个工具，把需求写成规则候选。"
)
_PLUGIN_PROPOSAL_DESCRIPTION = (
    "目录里没有能满足需求的触发器或动作时，选这个工具，描述缺失的能力。"
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


def _item_schema(
    type_ids: list[str], *, include_parameter_metadata: bool = False
) -> JSON:
    properties: dict[str, JSON] = {
        "name": {"type": "string"},
        "type": {"type": "string", "enum": type_ids},
        "label": {"type": "string"},
    }
    if include_parameter_metadata:
        properties.update({
            "default": {},
            "placeholder": {"type": "string"},
            "rows": {"type": "integer", "minimum": 1},
            "options": {"type": "array"},
            "min": {"type": "number"},
            "max": {"type": "number"},
            "step": {"type": "number"},
            "visible_when": {"type": "object"},
            "value_type": {"type": "string", "enum": sorted(_OUTPUT_TYPES)},
            "data_type": {"type": "string"},
            "required": {"type": "boolean"},
            "sensitive": {"type": "boolean"},
            "capture_only": {"type": "boolean"},
            "summary": {"type": "string", "enum": ["hidden", "shape", "value"]},
        })
    return {
        "type": "object",
        "properties": properties,
        "required": ["name", "type", "label"],
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


def _plugin_proposal_parameters() -> JSON:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["trigger", "action"]},
            "id": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "parameters": {
                "type": "array",
                "items": _item_schema(
                    sorted(_PARAM_TYPES), include_parameter_metadata=True
                ),
            },
            "outputs": {"type": "array", "items": _item_schema(sorted(_OUTPUT_TYPES))},
            "permissions": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(PERMISSION_REGISTRY)},
            },
            "rationale": {"type": "string"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["kind", "id", "name", "description"],
        "additionalProperties": False,
    }


def plugin_authoring_guidance() -> str:
    """生成插件源码时的静态创作准则，确定性、不读文件。"""
    param_types = ", ".join(sorted(_PARAM_TYPES))
    output_types = ", ".join(sorted(_OUTPUT_TYPES))
    permissions = ", ".join(sorted(PERMISSION_REGISTRY))
    return "\n".join([
        "插件源码创作准则（生成后会经安全审查再交给用户安装）：",
        "1. 一次只设计一个可配置的触发器或动作，用参数覆盖一类任务，不要为某个具体应用或任务写死插件。",
        "2. 禁止写死具体 QQ 号、窗口标题或用户路径，把它们做成参数交给用户填写。",
        "3. manifest 必填 id、name、description、enabled、version_code、version、package_name；"
        "触发器可加 semantic（state/oneshot）与 trigger_api（legacy/event-v1/event-v2），"
        "动作可加 execution_api（legacy/context-v1）。",
        "4. 触发器入口 run(meta, config, emit_event, shutdown_event)，用 emit_event(dict) 上报事件；"
        "动作入口 run(action_info, params)，context-v1 动作用 run_with_context(action_info, params, context)。",
        f"5. 参数类型仅限：{param_types}；输出类型仅限：{output_types}；权限仅限：{permissions}。",
        "6. ctypes 规范（违反会导致堆损坏崩溃，无 traceback）：所有 ctypes.windll 函数必须显式声明 argtypes 和 restype；"
        "多线程共享的原生段必须用 notmyfault.native.NATIVE_LOCK（RLock）包裹；禁止裸调用未声明类型的 ctypes 函数。",
        "7. 插件自带资源用 notmyfault.security.plugin_resources.plugin_resource(plugin_id, *path) 定位，"
        "禁止用 __file__ 自拼路径。",
        "8. 需要提权时在 manifest 声明 permissions: [\"admin\"]，调用 notmyfault.security.sudo.run_as_admin([...])，"
        "禁止直接调 ctypes ShellExecuteEx/CreateProcess 绕过授权通道。",
        "9. COM、音频驱动等崩溃风险高的原生库必须放子进程隔离（subprocess.run），"
        "子进程 stdin 用 sys.stdin.buffer.read().decode('utf-8') 读取，避免中文 Windows GBK 乱码。",
        "10. 目录布局：动作插件是 action.json + action.py，触发器插件是 trigger.json + trigger.py，"
        "都放在以插件 id 命名的文件夹里；manifest 不写 entrypoints 时引擎按这些文件名加载。",
        "动作 manifest 示例："
        '{"id":"my_tool","name":"我的工具","description":"做什么","enabled":true,'
        '"version_code":1,"version":"1.0.0","package_name":"com.example.my_tool",'
        '"params":[{"name":"target","label":"目标","type":"string","required":true}],'
        '"outputs":[{"name":"result","label":"结果","type":"string"}]}',
        "动作源码示例："
        "def run(action_info, params):\n"
        "    target = params.get('target', '')\n"
        "    return {'result': target.upper()}",
    ])


def build_rule_drafting_skill(
    catalog: PluginCatalog, *, allow_plugin_source: bool = False
) -> SkillSpec:
    tools: list[ToolSpec] = [
        ToolSpec(
            name="propose_rule_draft",
            description=_RULE_DRAFT_DESCRIPTION,
            parameters=_rule_draft_parameters(catalog),
        ),
        ToolSpec(
            name="propose_plugin",
            description=_PLUGIN_PROPOSAL_DESCRIPTION,
            parameters=_plugin_proposal_parameters(),
        ),
    ]
    if allow_plugin_source:
        tools.append(PLUGIN_SOURCE_TOOL)
    return SkillSpec(
        name="rule_drafting",
        description=_SKILL_DESCRIPTION,
        tools=tuple(tools),
    )
