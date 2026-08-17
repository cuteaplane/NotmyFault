"""规则候选与插件提案的工具参数解析。

parse_rule_draft 对着插件目录校验触发器/动作引用和参数名；
parse_plugin_proposal 校验提案的字段、id、权限和参数/输出条目，只产出 JSON 对象。
"""

from __future__ import annotations

import re
from typing import Mapping, TypeAlias

from notmyfault.host.ai_tools import (
    JSON,
    ToolCallError,
    _require_list,
    _require_object,
    _require_str,
)
from notmyfault.security.plugin_schema import is_known_permission

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


_RULE_FIELDS = frozenset({"name", "event", "actions"})
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
    return {
        "name": name,
        "event": _parse_node(event, triggers, "event", "触发器"),
        "actions": normalized,
    }


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


_PROPOSAL_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_PLUGIN_KINDS = frozenset({"trigger", "action"})
_PARAM_TYPES = frozenset({
    "string", "number", "select", "bool", "time", "hotkey", "path",
    "textarea", "uia_selector", "macro", "plugin_data",
})
_OUTPUT_TYPES = frozenset({"string", "number", "bool", "array", "object", "any"})
_ITEM_FIELDS = frozenset({"name", "type", "label"})
_PARAMETER_ITEM_FIELDS = _ITEM_FIELDS | frozenset({
    "default", "placeholder", "rows", "options", "min", "max", "step",
    "visible_when", "value_type", "data_type", "required", "sensitive",
    "capture_only", "summary",
})
_SUMMARY_POLICIES = frozenset({"hidden", "shape", "value"})


def _parse_item(
    item: JSON,
    location: str,
    allowed_types: frozenset[str],
    code: str,
    *,
    include_parameter_metadata: bool = False,
) -> dict[str, JSON]:
    if not isinstance(item, dict):
        raise ToolCallError(code, f"{location} 必须是对象")
    allowed_fields = (
        _PARAMETER_ITEM_FIELDS if include_parameter_metadata else _ITEM_FIELDS
    )
    _reject_extra_fields(item, allowed_fields, location, code)
    name = _require_str(item.get("name"), f"{location}.name", code)
    item_type = _require_str(item.get("type"), f"{location}.type", code)
    label = _require_str(item.get("label"), f"{location}.label", code)
    if item_type not in allowed_types:
        raise ToolCallError(code, f"{location}.type 无效: {item_type!r}")
    parsed: dict[str, JSON] = {"name": name, "type": item_type, "label": label}
    if include_parameter_metadata:
        _copy_parameter_metadata(item, parsed, location, code)
    return parsed


def _copy_parameter_metadata(
    item: dict[str, JSON],
    parsed: dict[str, JSON],
    location: str,
    code: str,
) -> None:
    if "default" in item:
        parsed["default"] = item["default"]
    for field in ("placeholder", "data_type"):
        if field in item:
            parsed[field] = _require_str(item[field], f"{location}.{field}", code)
    if "rows" in item:
        rows = item["rows"]
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 1:
            raise ToolCallError(code, f"{location}.rows 必须是正整数")
        parsed["rows"] = rows
    for field in ("min", "max", "step"):
        if field in item:
            value = item[field]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ToolCallError(code, f"{location}.{field} 必须是数字")
            parsed[field] = value
    if "options" in item:
        options = _require_list(item["options"], f"{location}.options", code)
        for option in options:
            if isinstance(option, str):
                continue
            if isinstance(option, dict) and isinstance(option.get("value"), str):
                continue
            raise ToolCallError(code, f"{location}.options 元素无效")
        parsed["options"] = options
    if "visible_when" in item:
        parsed["visible_when"] = _require_object(
            item["visible_when"], f"{location}.visible_when", code
        )
    if "value_type" in item:
        value_type = _require_str(
            item["value_type"], f"{location}.value_type", code
        )
        if value_type not in _OUTPUT_TYPES:
            raise ToolCallError(code, f"{location}.value_type 无效")
        parsed["value_type"] = value_type
    for field in ("required", "sensitive", "capture_only"):
        if field in item:
            if not isinstance(item[field], bool):
                raise ToolCallError(code, f"{location}.{field} 必须是布尔值")
            parsed[field] = item[field]
    if "summary" in item:
        summary = _require_str(item["summary"], f"{location}.summary", code)
        if summary not in _SUMMARY_POLICIES:
            raise ToolCallError(code, f"{location}.summary 无效")
        parsed["summary"] = summary


_PLUGIN_REQUIRED_FIELDS = frozenset({"kind", "id", "name", "description"})
_PLUGIN_OPTIONAL_FIELDS = frozenset({
    "parameters", "outputs", "permissions", "rationale", "acceptance_criteria",
})
_PLUGIN_ALLOWED_FIELDS = _PLUGIN_REQUIRED_FIELDS | _PLUGIN_OPTIONAL_FIELDS


def parse_plugin_proposal(args: Mapping[str, JSON]) -> Mapping[str, JSON]:
    _reject_extra_fields(args, _PLUGIN_ALLOWED_FIELDS, "proposal", "proposal_invalid")
    for field in sorted(_PLUGIN_REQUIRED_FIELDS):
        if field not in args:
            raise ToolCallError("proposal_invalid", f"proposal 缺少必填字段: {field}")
    kind = args["kind"]
    if kind not in _PLUGIN_KINDS:
        raise ToolCallError("proposal_invalid", f"proposal.kind 无效: {kind!r}")
    proposal: dict[str, JSON] = {"kind": kind}
    for field in ("name", "description"):
        proposal[field] = _require_str(args[field], f"proposal.{field}", "proposal_invalid")
    plugin_id = _require_str(args["id"], "proposal.id", "proposal_invalid")
    if not _PROPOSAL_ID_RE.match(plugin_id):
        raise ToolCallError("proposal_invalid", f"proposal.id 格式无效: {plugin_id!r}")
    proposal["id"] = plugin_id
    if "permissions" in args:
        permissions = _require_list(
            args["permissions"], "proposal.permissions", "proposal_invalid"
        )
        for index, perm in enumerate(permissions):
            if not isinstance(perm, str):
                raise ToolCallError(
                    "proposal_invalid", f"proposal.permissions[{index}] 必须是字符串"
                )
            if not is_known_permission(perm):
                raise ToolCallError(
                    "proposal_invalid", f"proposal.permissions[{index}] 未知权限: {perm}"
                )
        proposal["permissions"] = permissions
    if "parameters" in args:
        parameters = _require_list(
            args["parameters"], "proposal.parameters", "proposal_invalid"
        )
        proposal["parameters"] = [
            _parse_item(
                item,
                f"proposal.parameters[{index}]",
                _PARAM_TYPES,
                "proposal_invalid",
                include_parameter_metadata=True,
            )
            for index, item in enumerate(parameters)
        ]
    if "outputs" in args:
        outputs = _require_list(
            args["outputs"], "proposal.outputs", "proposal_invalid"
        )
        proposal["outputs"] = [
            _parse_item(item, f"proposal.outputs[{index}]", _OUTPUT_TYPES, "proposal_invalid")
            for index, item in enumerate(outputs)
        ]
    if "rationale" in args:
        proposal["rationale"] = _require_str(
            args["rationale"], "proposal.rationale", "proposal_invalid"
        )
    if "acceptance_criteria" in args:
        criteria = _require_list(
            args["acceptance_criteria"], "proposal.acceptance_criteria", "proposal_invalid"
        )
        if any(not isinstance(item, str) for item in criteria):
            raise ToolCallError(
                "proposal_invalid", "proposal.acceptance_criteria 必须是字符串数组"
            )
        proposal["acceptance_criteria"] = criteria
    return proposal
