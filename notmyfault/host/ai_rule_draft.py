"""规则草稿工具参数解析。"""

from __future__ import annotations

from typing import Mapping, TypeAlias

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


_RULE_FIELDS = frozenset({"name", "event", "preconditions", "actions"})
_NODE_FIELDS = frozenset({"type", "params"})


def parse_rule_draft(
    args: Mapping[str, JSON], catalog: PluginCatalog
) -> Mapping[str, JSON]:
    _reject_extra_fields(args, _RULE_FIELDS, "rule", "rule_invalid")
    name = _require_str(args.get("name"), "rule.name", "rule_invalid")
    event = _require_object(args.get("event"), "rule.event", "rule_invalid")
    actions = _require_list(
        args.get("actions"), "rule.actions", "rule_invalid", nonempty=True
    )
    raw_preconditions = args.get("preconditions")
    if raw_preconditions is None:
        raw_preconditions = []
    preconditions = _require_list(
        raw_preconditions, "rule.preconditions", "rule_invalid"
    )
    triggers = _catalog_map(catalog, "triggers")
    actions_meta = _catalog_map(catalog, "actions")
    normalized: list[JSON] = []
    for index, action in enumerate(actions):
        location = f"actions[{index}]"
        normalized.append(
            _parse_node(
                _require_object(action, location, "rule_invalid"),
                actions_meta,
                location,
                "动作",
            )
        )
    normalized_preconditions: list[JSON] = []
    for index, precondition in enumerate(preconditions):
        location = f"preconditions[{index}]"
        normalized_preconditions.append(
            _parse_node(
                _require_object(precondition, location, "rule_invalid"),
                actions_meta,
                location,
                "检查",
            )
        )
    draft: dict[str, JSON] = {
        "name": name,
        "event": _parse_node(event, triggers, "event", "触发器"),
        "actions": normalized,
    }
    if normalized_preconditions:
        draft["preconditions"] = normalized_preconditions
    return draft


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
