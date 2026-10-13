"""规则草稿工具参数解析。"""

from __future__ import annotations

from typing import Mapping, TypeAlias
from notmyfault.core.rule_model import normalize_rule_shape
from notmyfault.core.workflow_graph import get_all_rule_events, iter_rule_actions

from notmyfault.host.ai_tools import (
    JSON,
    ToolCallError,
    _require_list,
    _require_object,
    _require_str,
)

PluginCatalog: TypeAlias = Mapping[str, Mapping[str, JSON]]


def _reject_extra_fields(
    value: Mapping[str, JSON],
    allowed: frozenset[str],
    location: str,
    code: str,
    label: str = "字段",
) -> None:
    for key in value:
        if key not in allowed:
            raise ToolCallError(code, f"{location} 含未知{label}: {key}")


def _allowed_param_names(meta: JSON) -> frozenset[str]:
    if not isinstance(meta, dict):
        return frozenset()
    params = meta.get("params")
    if isinstance(params, list):
        names: set[str] = set()
        for item in params:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str):
                names.add(name)
        return frozenset(names)
    if isinstance(params, dict):
        return frozenset(name for name in params if isinstance(name, str))
    return frozenset()


def _catalog_map(catalog: PluginCatalog, key: str) -> dict[str, JSON]:
    value = catalog.get(key)
    if not isinstance(value, dict):
        raise ToolCallError("rule_invalid", f"插件目录缺少 {key}")
    return value


_NODE_FIELDS = frozenset({"type", "params"})


def parse_rule_draft(args: Mapping[str, JSON], catalog: PluginCatalog) -> Mapping[str, JSON]:
    try:
        args = normalize_rule_shape(dict(args))
    except ValueError as error:
        raise ToolCallError("rule_invalid", str(error)) from error
    return _parse_workflow_draft(args, catalog)


def _parse_workflow_draft(args, catalog):
    from notmyfault.core.rules import validate_rule_structure

    _reject_extra_fields(args, frozenset({"name", "workflow", "constants", "variables", "concurrency"}), "rule", "rule_invalid")
    _require_str(args.get("name"), "rule.name", "rule_invalid")
    _require_object(args.get("workflow"), "rule.workflow", "rule_invalid")
    errors = validate_rule_structure(args)
    if errors:
        raise ToolCallError("rule_invalid", "；".join(errors[:10]))
    triggers, actions = _catalog_map(catalog, "triggers"), _catalog_map(catalog, "actions")
    for index, trigger in enumerate(get_all_rule_events(args)):
        _reject_extra_fields(trigger, frozenset({"type", "params", "binding_id"}), f"triggers[{index}]", "rule_invalid")
        _parse_node({key: trigger[key] for key in ("type", "params") if key in trigger}, triggers, f"triggers[{index}]", "触发器")
    common = {"binding_id", "type", "name", "parent", "position"}
    native_fields = {
        "trigger": {"trigger", "occurrence", "absence_scope", "duration_seconds", "repeat"},
        "condition": {"predicate"},
        "logic": {"op", "negated", "within_seconds", "duration_seconds", "consume"},
        "event_group": {"group_type", "branches", "negated", "within_seconds", "duration_seconds", "consume", "mode", "timeout_seconds"},
        "action_group": {"exports"},
        "set_variable": {"variable", "value", "on_error"},
    }
    plugin_fields = {"binding_id", "type", "params", "retry", "retry_delay_seconds", "retry_backoff", "timeout_seconds", "on_error", "failure_actions"}
    for action, location in iter_rule_actions(args):
        action_type = action["type"]
        _reject_extra_fields(action, frozenset(common | native_fields.get(action_type, plugin_fields)), location, "rule_invalid")
        if action_type == "event_group":
            for index, branch in enumerate(action.get("branches", [])):
                _reject_extra_fields(branch, frozenset({"id", "label", "negated", "else"}), f"{location}.branches[{index}]", "rule_invalid")
        elif action_type not in native_fields:
            _parse_node({key: action[key] for key in ("type", "params") if key in action}, actions, location, "动作")
    return dict(args)


def _parse_node(
    node: dict[str, JSON],
    catalog_map: dict[str, JSON],
    location: str,
    kind_label: str,
) -> Mapping[str, JSON]:
    _reject_extra_fields(node, _NODE_FIELDS, location, "rule_invalid")
    node_type = _require_str(node.get("type"), f"{location}.type", "rule_invalid")
    if node_type not in catalog_map:
        raise ToolCallError("rule_invalid", f"{location} 引用了未知{kind_label}: {node_type}")
    params = _require_object(node.get("params"), f"{location}.params", "rule_invalid")
    _reject_extra_fields(
        params,
        _allowed_param_names(catalog_map[node_type]),
        f"{location}.params",
        "rule_invalid",
        "参数",
    )
    return {"type": node_type, "params": dict(params)}
