from __future__ import annotations

import copy
import re
import secrets
from typing import Any, Dict, List

from notmyfault.core.bindings import is_reference, is_literal, is_typed_value

_BINDING_ID_RE = re.compile(r"^[tap]_[a-z0-9_]{6,64}$")
_RULE_ID_RE = re.compile(r"^r_[a-z0-9_]{6,64}$")
_LEGACY_TEMPLATE_RE = re.compile(r"{{\s*([a-zA-Z_][\w.]*)\s*}}")


def _new_binding_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"

def ensure_rule_id(
    rule: Dict[str, Any],
    seen: set[str] | None = None,
) -> Dict[str, Any]:
    """给规则补充跨保存和重排保持不变的身份"""
    copied = dict(rule)
    used = seen if seen is not None else set()
    rule_id = copied.get("rule_id")
    if (
        not isinstance(rule_id, str)
        or not _RULE_ID_RE.fullmatch(rule_id)
        or rule_id in used
    ):
        rule_id = f"r_{secrets.token_hex(6)}"
        while rule_id in used:
            rule_id = f"r_{secrets.token_hex(6)}"
    copied["rule_id"] = rule_id
    used.add(rule_id)
    return copied

def _ensure_condition_binding_ids(
    condition: Any,
    seen: set[str],
) -> Any:
    """给条件树叶子补充持久、可被动作引用的运行时身份"""
    if not isinstance(condition, dict):
        return condition
    copied = dict(condition)
    children = copied.get("children")
    if isinstance(children, list):
        copied["children"] = [
            _ensure_condition_binding_ids(child, seen) for child in children
        ]
        return copied

    binding_id = copied.get("binding_id")
    if (
        not isinstance(binding_id, str)
        or not _BINDING_ID_RE.fullmatch(binding_id)
        or not binding_id.startswith("t_")
        or binding_id in seen
    ):
        binding_id = _new_binding_id("t")
    copied["binding_id"] = binding_id
    seen.add(binding_id)
    return copied

def ensure_rule_binding_ids(rule: Dict[str, Any]) -> Dict[str, Any]:
    """规范化一条规则中可产生/消费运行数据的节点身份"""
    copied = normalize_rule_shape(rule)
    seen: set[str] = set()
    if isinstance(copied.get("condition"), dict):
        copied["condition"] = _ensure_condition_binding_ids(
            copied["condition"], seen
        )

    def normalize_items(items: Any, prefix: str, *, include_failures: bool) -> Any:
        if not isinstance(items, list):
            return items
        normalized = []
        for item in items:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            item_copy = dict(item)
            binding_id = item_copy.get("binding_id")
            if (
                not isinstance(binding_id, str)
                or not _BINDING_ID_RE.fullmatch(binding_id)
                or not binding_id.startswith(f"{prefix}_")
                or binding_id in seen
            ):
                binding_id = _new_binding_id(prefix)
            item_copy["binding_id"] = binding_id
            seen.add(binding_id)
            if include_failures and "failure_actions" in item_copy:
                item_copy["failure_actions"] = normalize_items(
                    item_copy["failure_actions"], "a", include_failures=False
                )
            if item_copy.get("type") == "if":
                for branch in ("then", "else"):
                    if branch in item_copy:
                        item_copy[branch] = normalize_items(
                            item_copy[branch], "a", include_failures=include_failures
                        )
            normalized.append(item_copy)
        return normalized

    for field, prefix in (("preconditions", "p"), ("actions", "a")):
        items = copied.get(field)
        if not isinstance(items, list):
            continue
        copied[field] = normalize_items(
            items, prefix, include_failures=field == "actions"
        )
    return copied

def _normalize_condition(condition: Any) -> Any:
    """把旧条件树转换成统一的 op 和 children 格式"""
    if not isinstance(condition, dict):
        return condition

    copied = dict(condition)
    children = copied.get("children", copied.get("events"))
    # 带 children 或 events 的节点是条件组，叶子的 type 字段必须保留
    if not isinstance(children, list):
        return copied

    op = copied.get("op", copied.get("type", "any"))
    copied["op"] = {"and": "all", "or": "any"}.get(op, op) if isinstance(op, str) else op
    copied["children"] = [_normalize_condition(child) for child in children]
    copied.pop("events", None)
    copied.pop("type", None)
    if copied["op"] == "any":
        # any 条件组不使用 within_seconds，旧界面只隐藏过这个字段
        copied.pop("within_seconds", None)
    return copied

def _unwrap_single_condition(condition: Any) -> Dict[str, Any] | None:
    """从单分支条件组中取出事件，消除旧版重复字段"""
    current = condition
    while isinstance(current, dict):
        if current.get("op") == "not":
            return None
        children = current.get("children")
        if isinstance(children, list):
            if len(children) != 1:
                return None
            current = children[0]
            continue
        return current if isinstance(current.get("type"), str) else None
    return None


def normalize_rule_shape(rule: Dict[str, Any]) -> Dict[str, Any]:
    copied = dict(rule)
    legacy_fields = [copied.pop(name) for name in ("event", "trigger") if name in copied]
    legacy = legacy_fields[0] if legacy_fields else None
    if len(legacy_fields) == 2 and legacy != legacy_fields[1]:
        raise ValueError("event 与 trigger 的触发条件不一致")
    if "condition" in copied:
        condition = _normalize_condition(copied["condition"])
        if legacy_fields and _unwrap_single_condition(condition) != legacy:
            raise ValueError("event/trigger 与 condition 不能同时存在")
        copied["condition"] = condition
    elif legacy_fields:
        copied["condition"] = _normalize_condition(legacy)
    return copied

def _replace_step_references(value: Any, replacements: Dict[str, str]) -> Any:
    if is_literal(value) or is_typed_value(value):
        return copy.deepcopy(value)
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            parts = match.group(1).split(".")
            if len(parts) >= 3 and parts[0] == "steps" and parts[1] in replacements:
                parts[1] = replacements[parts[1]]
                return match.group(0).replace(match.group(1), ".".join(parts), 1)
            return match.group(0)

        return _LEGACY_TEMPLATE_RE.sub(replace, value)
    if isinstance(value, list):
        return [_replace_step_references(item, replacements) for item in value]
    if isinstance(value, dict):
        if is_reference(value):
            reference = dict(value["$ref"])
            if reference.get("scope") == "step":
                node = reference.get("node")
                if node in replacements:
                    reference["node"] = replacements[node]
            return {"$ref": reference}
        return {key: _replace_step_references(item, replacements) for key, item in value.items()}
    return value

def _normalize_rule_actions(actions: Any) -> Any:
    """移除旧步骤 ID，并把能确定的旧引用改为自动步骤名"""
    if not isinstance(actions, list):
        return actions

    ids: Dict[str, str] = {}
    duplicate_ids = set()
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        old_id = action.get("id")
        if not isinstance(old_id, str) or not old_id:
            continue
        generated = f"{action.get('type', 'action')}_{index + 1}"
        if old_id in ids:
            duplicate_ids.add(old_id)
        else:
            ids[old_id] = generated
    replacements = {old: new for old, new in ids.items() if old not in duplicate_ids}

    normalized = []
    for action in actions:
        if not isinstance(action, dict):
            normalized.append(action)
            continue
        copied = dict(action)
        copied.pop("id", None)
        for branch in ("then", "else", "failure_actions"):
            if branch in copied:
                copied[branch] = _normalize_rule_actions(copied[branch])
        # display_control 的旧亮度动作在加载时转换为新参数，旧规则仍可执行
        if copied.get("type") == "display_control":
            params = copied.get("params")
            if isinstance(params, dict):
                legacy_action = params.get("action")
                if legacy_action in ("low_brightness", "high_brightness"):
                    copied_params = dict(params)
                    copied_params["action"] = "set_brightness"
                    copied_params["brightness"] = (
                        10 if legacy_action == "low_brightness" else 90
                    )
                    copied["params"] = copied_params
        normalized.append(_replace_step_references(copied, replacements))
    return normalized

def _legacy_template_ref(
    dotted_path: str,
    step_refs: Dict[str, str],
) -> Dict[str, Any] | None:
    """把旧模板路径解析为结构化 $ref，无法定位来源时返回 None"""
    parts = dotted_path.split(".")
    if len(parts) >= 3 and parts[:2] == ["event", "payload"]:
        return {"scope": "event", "path": parts[2:]}
    if len(parts) >= 4 and parts[0] == "steps" and parts[2] == "result":
        new_id = step_refs.get(parts[1])
        if new_id is None:
            return None
        return {"scope": "step", "node": new_id, "path": parts[3:]}
    return None

def _upgrade_legacy_templates(
    value: Any,
    step_refs: Dict[str, str],
) -> Any:
    """把纯模板字符串转换为结构化 $ref，并更新混合模板中的步骤 ID"""
    if is_literal(value) or is_typed_value(value):
        return copy.deepcopy(value)
    if isinstance(value, str):
        full = _LEGACY_TEMPLATE_RE.fullmatch(value)
        if full:
            reference = _legacy_template_ref(full.group(1), step_refs)
            if reference is not None:
                return {"$ref": reference}
        # 混合模板需要更新旧步骤 ID 引用
        return _replace_step_references(value, step_refs)
    if isinstance(value, list):
        return [_upgrade_legacy_templates(item, step_refs) for item in value]
    if isinstance(value, dict):
        if is_reference(value):
            return _replace_step_references(value, step_refs)
        return {
            key: _upgrade_legacy_templates(item, step_refs)
            for key, item in value.items()
        }
    return value

def _upgrade_rule_templates(
    rule: Dict[str, Any], *, inherited: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """升级规则动作和确认参数中的旧模板引用"""
    step_refs: Dict[str, str] = dict(inherited or {})
    actions = rule.get("actions")
    if isinstance(actions, list):
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            legacy_key = f"{action.get('type', 'action')}_{index + 1}"
            step_refs[legacy_key] = action.get("binding_id", legacy_key)

    copied = dict(rule)
    for field in ("preconditions", "actions"):
        items = copied.get(field)
        if not isinstance(items, list):
            continue
        normalized = []
        for item in items:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            item_copy = dict(item)
            for value_field in ("params", "condition", "value"):
                if value_field in item_copy:
                    item_copy[value_field] = _upgrade_legacy_templates(item_copy[value_field], step_refs)
            for branch in ("then", "else", "failure_actions"):
                if isinstance(item_copy.get(branch), list):
                    item_copy[branch] = _upgrade_rule_templates(
                        {"actions": item_copy[branch]}, inherited=step_refs
                    )["actions"]
            normalized.append(item_copy)
        copied[field] = normalized
    return copied

def normalize_rules(rules: Any) -> List[Dict[str, Any]]:
    """规范化规则列表并补齐规则和节点身份"""
    if not isinstance(rules, list):
        raise ValueError("rules 必须是列表")
    normalized_rules = []
    seen_rule_ids: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("规则必须是对象")
        copied = normalize_rule_shape(rule)
        if "actions" in copied:
            copied["actions"] = _normalize_rule_actions(copied["actions"])
        normalized = ensure_rule_id(copied, seen_rule_ids)
        normalized = ensure_rule_binding_ids(normalized)
        normalized_rules.append(_upgrade_rule_templates(normalized))
    return normalized_rules

def _extract_legacy_rules(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从旧 config 提取规则，兼容更早的进程列表格式"""
    if isinstance(config.get("rules"), list):
        return normalize_rules(config["rules"])

    processes = config.get("processes")
    if not isinstance(processes, list):
        return []

    rules: List[Dict[str, Any]] = []
    for process in processes:
        if not isinstance(process, dict):
            continue

        process_name = process.get("process_name", "")
        software_name = process.get("software_name", process_name)
        volume_action = process.get("volume_action", "max")
        notification = process.get("notification", {}) or {}

        _rule = {
                "name": f"{software_name} 音量规则",
                "event": {
                    "type": "process_state",
                    "params": {
                        "process_name": process_name,
                        "state": "running"
                    }
                },
                "actions": [
                    {"type": "set_volume", "params": {"action": volume_action}},
                    {
                        "type": "notify",
                        "params": {
                            "title": notification.get("title", f"{software_name} 正在运行"),
                            "message": notification.get("message", "")
                        }
                    }
                ]
            }
        rules.append(_rule)

    return normalize_rules(rules)
