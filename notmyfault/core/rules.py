"""规则引擎的纯函数负责条件树、事件匹配、规则校验和触发器参数聚合"""
import json
import copy
import threading
import time
import re
from typing import Any, Dict, Iterable, List, Tuple

from notmyfault.core.bindings import is_reference, iter_references


_BINDING_ID_RE = re.compile(r"^[tap]_[a-z0-9_]{6,64}$")

def get_rule_condition(rule: Dict[str, Any]) -> Dict[str, Any] | None:
    """返回规则条件树并兼容旧版扁平 event 和 trigger 字段"""
    condition = rule.get("condition")
    if isinstance(condition, dict):
        return condition
    event = rule.get("event") or rule.get("trigger")
    return event if isinstance(event, dict) else None


def _is_event_leaf(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("type"), str) and \
        "children" not in node and "events" not in node


def _condition_children(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取条件节点子项，兼容旧的 events 字段"""
    children = node.get("children")
    if not isinstance(children, list):
        children = node.get("events", [])
    return [child for child in children if isinstance(child, dict)]


def iter_condition_events(node: Dict[str, Any] | None) -> Iterable[Dict[str, Any]]:
    """深度优先枚举条件树中的事件叶子"""
    if not isinstance(node, dict):
        return
    if _is_event_leaf(node):
        yield node
        return
    for child in _condition_children(node):
        yield from iter_condition_events(child)


def get_rule_events(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从规则的任意条件树中提取所有事件条件"""
    return list(iter_condition_events(get_rule_condition(rule)))


def validate_condition_tree(node: Dict[str, Any] | None) -> List[str]:
    """校验可序列化的条件树结构和运算符"""
    errors: List[str] = []

    def visit(current: Any, path: str) -> None:
        if not isinstance(current, dict):
            errors.append(f"{path} 必须是对象")
            return
        if _is_event_leaf(current):
            if not current.get("type"):
                errors.append(f"{path}.type 不能为空")
            if "params" in current and not isinstance(current["params"], dict):
                errors.append(f"{path}.params 必须是对象")
            return
        op = _condition_op(current)
        if op not in ("any", "all"):
            errors.append(f"{path} 的 op 必须为 any 或 all")
        children = _condition_children(current)
        if not children:
            errors.append(f"{path} 至少需要一个子条件")
        for index, child in enumerate(children):
            visit(child, f"{path}.children[{index}]")
        if "within_seconds" in current:
            try:
                if float(current["within_seconds"]) <= 0:
                    errors.append(f"{path}.within_seconds 必须大于 0")
            except (TypeError, ValueError):
                errors.append(f"{path}.within_seconds 必须是数字")

    visit(node, "condition")
    return errors


def validate_rule_structure(rule: Any) -> List[str]:
    """校验规则结构是否符合引擎输入格式"""
    errors: List[str] = []
    if not isinstance(rule, dict):
        return ["规则必须是对象"]

    if not str(rule.get("name", "")).strip():
        errors.append("name 不能为空")

    has_event = "event" in rule or "trigger" in rule
    has_condition = "condition" in rule
    if has_event and has_condition:
        errors.append("event/trigger 与 condition 不能同时存在")
    elif not has_event and not has_condition:
        errors.append("必须配置 event 或 condition")
    else:
        errors.extend(validate_condition_tree(get_rule_condition(rule)))

    actions = rule.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("actions 至少需要一个动作")
    else:
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                errors.append(f"actions[{index}] 必须是对象")
                continue
            if not str(action.get("type", "")).strip():
                errors.append(f"actions[{index}].type 不能为空")
            if "params" in action and not isinstance(action["params"], dict):
                errors.append(f"actions[{index}].params 必须是对象")

    preconditions = rule.get("preconditions", [])
    if not isinstance(preconditions, list):
        errors.append("preconditions 必须是列表")
    else:
        for index, item in enumerate(preconditions):
            if not isinstance(item, dict):
                errors.append(f"preconditions[{index}] 必须是对象")
                continue
            if not str(item.get("type", "")).strip():
                errors.append(f"preconditions[{index}].type 不能为空")
            if "params" in item and not isinstance(item["params"], dict):
                errors.append(f"preconditions[{index}].params 必须是对象")

    return errors


def _validate_node_binding_id(
    node: Dict[str, Any],
    prefix: str,
    path: str,
    seen: set[str],
    errors: List[str],
) -> None:
    binding_id = node.get("binding_id")
    if not isinstance(binding_id, str) or not _BINDING_ID_RE.fullmatch(binding_id):
        errors.append(f"{path}.binding_id 无效")
        return
    if not binding_id.startswith(f"{prefix}_"):
        errors.append(f"{path}.binding_id 必须以 {prefix}_ 开头")
    if binding_id in seen:
        errors.append(f"{path}.binding_id 与其他节点重复")
    seen.add(binding_id)


def validate_rule_binding_ids(rule: Dict[str, Any]) -> List[str]:
    """校验 v2 节点身份，v1 规则的补齐由配置迁移器完成"""
    errors: List[str] = []
    seen: set[str] = set()

    def visit(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        if _is_event_leaf(node):
            _validate_node_binding_id(node, "t", path, seen, errors)
            return
        for index, child in enumerate(_condition_children(node)):
            visit(child, f"{path}.children[{index}]")

    condition = get_rule_condition(rule)
    if condition is not None:
        visit(condition, "condition" if "condition" in rule else "event")
    for field, prefix in (("preconditions", "p"), ("actions", "a")):
        items = rule.get(field, [])
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if isinstance(item, dict):
                _validate_node_binding_id(
                    item, prefix, f"{field}[{index}]", seen, errors
                )
    return errors


def validate_rules_structure(rules: Any) -> List[str]:
    """校验规则列表并返回带索引的错误供写入入口复用"""
    if not isinstance(rules, list):
        return ["rules 必须是列表"]
    errors: List[str] = []
    for index, rule in enumerate(rules):
        name = rule.get("name") if isinstance(rule, dict) else None
        label = str(name).strip() if name else f"#{index + 1}"
        errors.extend(
            f"规则 {label}: {error}"
            for error in (
                validate_rule_structure(rule)
                + (
                    validate_rule_binding_ids(rule)
                    if isinstance(rule, dict)
                    and any(
                        "binding_id" in node
                        for node in (
                            get_rule_events(rule)
                            + [
                                item for field in ("preconditions", "actions")
                                for item in rule.get(field, [])
                                if isinstance(item, dict)
                            ]
                        )
                    )
                    else []
                )
            )
        )
    return errors


def check_event_params(event_def: Dict[str, Any], event_payload: Dict[str, Any]) -> bool:
    """检查事件参数是否匹配并允许 payload 含额外字段"""
    expected_params = event_def.get("params", {})
    for key, expected_val in expected_params.items():
        # 缺少字段时不算命中，多个规则只在字段完整时触发
        if key not in event_payload or event_payload[key] != expected_val:
            return False
    return True


def _canonical_config_value(value: Any) -> Any:
    """递归规范化配置值，等价写法使用相同指纹"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return int(value) if float(value).is_integer() else value
        except (OverflowError, ValueError):
            return value
    if isinstance(value, dict):
        return {str(key): _canonical_config_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_config_value(item) for item in value]
    return value


def config_fingerprint(params: Any) -> str:
    """生成 event-v2 触发器实例配置的稳定指纹"""
    if not isinstance(params, dict):
        params = {}
    return json.dumps(
        _canonical_config_value(params),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _condition_op(node: Dict[str, Any]) -> str:
    """读取条件运算符，兼容 ``type: and/or`` 的早期格式"""
    op = node.get("op", node.get("type", "any"))
    return {"or": "any", "and": "all"}.get(str(op).lower(), str(op).lower())


def _event_key(event_def: Dict[str, Any]) -> str:
    """生成事件叶子的稳定键，优先使用 binding_id"""
    binding_id = event_def.get("binding_id")
    if isinstance(binding_id, str) and binding_id:
        return f"id:{binding_id}"
    return json.dumps(
        {"type": event_def.get("type", ""), "params": event_def.get("params", {})},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class ConditionRuntime:
    """维护条件树的命中状态并判断新的组合"""

    def __init__(self) -> None:
        self._seen: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._fired: Dict[str, tuple[tuple[str, float], ...]] = {}
        self._last_matches: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()
            self._fired.clear()
            self._last_matches.clear()

    def last_match(self, rule_key: str) -> List[Dict[str, Any]]:
        """返回最近一次命中的事件供动作执行"""
        with self._lock:
            return [
                {
                    **item,
                    "event": dict(item["event"]),
                    "payload": dict(item["payload"]),
                }
                for item in self._last_matches.get(rule_key, [])
            ]

    def match(
        self,
        rule_key: str,
        rule: Dict[str, Any],
        event_type: str,
        event_payload: Dict[str, Any],
        now: float | None = None,
        instance: Dict[str, Any] | None = None,
    ) -> bool:
        """记录事件并按 event-v1 或 event-v2 规则判断新的命中组合"""
        node = get_rule_condition(rule)
        if node is None:
            return False
        timestamp = time.monotonic() if now is None else now
        if instance is not None:
            fingerprint = config_fingerprint(instance.get("config"))
            matching_leaves = [
                leaf for leaf in iter_condition_events(node)
                if leaf.get("type") == event_type
                and config_fingerprint(leaf.get("params")) == fingerprint
            ]
        else:
            matching_leaves = [
                leaf for leaf in iter_condition_events(node)
                if leaf.get("type") == event_type and check_event_params(leaf, event_payload)
            ]
        if not matching_leaves:
            return False

        with self._lock:
            seen = self._seen.setdefault(rule_key, {})
            for leaf in matching_leaves:
                seen[_event_key(leaf)] = {
                    "binding_id": leaf.get("binding_id"),
                    "event": {
                        "type": leaf.get("type", ""),
                        "params": dict(leaf.get("params", {})),
                    },
                    "timestamp": timestamp,
                    "payload": dict(event_payload),
                }

            matched, signature = self._evaluate(node, seen)
            if not matched:
                return False
            # 同一批 AND 命中只执行一次，直到新的事件形成新组合
            if self._fired.get(rule_key) == signature:
                return False
            self._fired[rule_key] = signature
            self._last_matches[rule_key] = [
                dict(seen[key])
                for key, fired_at in signature
                if key in seen
            ]
            return True

    def _evaluate(
        self,
        node: Dict[str, Any],
        seen: Dict[str, Dict[str, Any]],
    ) -> tuple[bool, tuple[tuple[str, float], ...]]:
        if _is_event_leaf(node):
            key = _event_key(node)
            entry = seen.get(key)
            return (
                entry is not None,
                ((key, float(entry["timestamp"])),) if entry else (),
            )

        children = _condition_children(node)
        if not children:
            return False, ()
        states = [self._evaluate(child, seen) for child in children]
        op = _condition_op(node)
        if op == "all":
            if not all(ok for ok, _signature in states):
                return False, ()
            signature = tuple(item for _ok, part in states for item in part)
            window = node.get("within_seconds")
            if window not in (None, ""):
                try:
                    limit = float(window)
                except (TypeError, ValueError):
                    return False, ()
                timestamps = [item[1] for item in signature]
                if limit < 0 or (timestamps and max(timestamps) - min(timestamps) > limit):
                    return False, ()
            return True, tuple(sorted(signature))

        # any 条件组选最近命中的分支，缓存顺序不再决定 OR 组合
        matches = [signature for ok, signature in states if ok]
        if matches:
            latest = max(
                matches,
                key=lambda signature: max((item[1] for item in signature), default=float("-inf")),
            )
            return True, latest
        return False, ()


def aggregate_trigger_params(rules: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """把同一 trigger 的参数聚合成列表，并复制字典隔离规则配置"""
    aggregated: Dict[str, List[Dict[str, Any]]] = {}
    for rule in rules:
        for event_def in get_rule_events(rule):
            event_type = event_def.get("type")
            if not event_type:
                continue
            aggregated.setdefault(event_type, []).append(copy.deepcopy(event_def.get("params", {})))
    return aggregated


def _normalized_outputs(meta: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for item in meta.get("outputs", []):
        if isinstance(item, str) and item:
            result[item] = {
                "name": item,
                "label": item,
                "type": "any",
                "required": True,
            }
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            result[item["name"]] = {
                "required": True,
                "sensitive": False,
                **item,
            }
    return result


def guaranteed_trigger_ids(node: Dict[str, Any] | None) -> set[str]:
    """返回条件树每次成立时都必然出现的触发器节点"""
    if not isinstance(node, dict):
        return set()
    if _is_event_leaf(node):
        binding_id = node.get("binding_id")
        return {binding_id} if isinstance(binding_id, str) else set()
    children = _condition_children(node)
    if not children:
        return set()
    child_sets = [guaranteed_trigger_ids(child) for child in children]
    if _condition_op(node) == "all":
        return set().union(*child_sets)
    result = set(child_sets[0])
    for child_set in child_sets[1:]:
        result.intersection_update(child_set)
    return result


def _source_type(
    output: Dict[str, Any] | None,
) -> str:
    return str(output.get("type", "any")) if output else "any"


def _target_type(param: Dict[str, Any] | None) -> str:
    if not param:
        return "any"
    raw = param.get("value_type", param.get("type", "string"))
    if raw in ("string", "textarea", "path", "time", "hotkey", "select"):
        return "string"
    return str(raw)


def _types_compatible(source: str, target: str) -> bool:
    return source == "any" or target == "any" or source == target


def validate_rule_bindings(
    rule: Dict[str, Any],
    triggers_meta: Dict[str, Dict[str, Any]],
    actions_meta: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """静态验证结构化 ``$ref`` 的来源、顺序、可用性和类型"""
    issues: List[Dict[str, Any]] = []
    leaves = get_rule_events(rule)
    leaves_by_id = {
        item.get("binding_id"): item
        for item in leaves
        if isinstance(item.get("binding_id"), str)
    }
    guaranteed = guaranteed_trigger_ids(get_rule_condition(rule))
    actions = [
        item for item in rule.get("actions", []) if isinstance(item, dict)
    ]
    actions_by_id = {
        item.get("binding_id"): (index, item)
        for index, item in enumerate(actions)
        if isinstance(item.get("binding_id"), str)
    }

    def add(code: str, usage: Any, message: str) -> None:
        issues.append({
            "code": code,
            "location": usage.location,
            "reference": usage.reference,
            "message": message,
        })

    def output_for(
        meta: Dict[str, Any],
        reference: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        path = reference.get("path", [])
        if not isinstance(path, list) or not path:
            return None
        return _normalized_outputs(meta).get(path[0])

    def validate_item(
        item: Dict[str, Any],
        *,
        item_index: int,
        field: str,
        allow_steps: bool,
        allow_conditional_sources: bool,
    ) -> None:
        action_meta = actions_meta.get(item.get("type", ""), {})
        target_params = {
            param.get("name"): param
            for param in action_meta.get("params", [])
            if isinstance(param, dict)
        }
        params = item.get("params", {})
        if not isinstance(params, dict):
            return
        for param_name, value in params.items():
            for usage in iter_references(
                value,
                location=f"{field}[{item_index}].params.{param_name}",
            ):
                if item.get("type") == "run_powershell" and param_name == "command":
                    add(
                        "unsafe_dynamic_parameter",
                        usage,
                        "PowerShell 命令不允许来自运行时数据",
                    )
                    continue
                reference = usage.reference
                scope = reference.get("scope")
                path = reference.get("path")
                if (
                    not isinstance(path, list)
                    or any(not isinstance(segment, str) for segment in path)
                ):
                    add("invalid_reference", usage, "$ref.path 必须是字符串数组")
                    continue

                source_output: Dict[str, Any] | None = None
                if scope == "trigger":
                    node_id = reference.get("node")
                    leaf = leaves_by_id.get(node_id)
                    if leaf is None:
                        add("unknown_source", usage, "引用的触发条件不存在")
                        continue
                    if node_id not in guaranteed and not allow_conditional_sources:
                        add(
                            "conditional_source",
                            usage,
                            "该触发条件并非每次规则运行都会命中",
                        )
                        continue
                    source_output = output_for(
                        triggers_meta.get(leaf.get("type", ""), {}),
                        reference,
                    )
                elif scope == "trigger_config":
                    node_id = reference.get("node")
                    leaf = leaves_by_id.get(node_id)
                    if leaf is None:
                        add("unknown_source", usage, "引用的触发条件不存在")
                        continue
                    if node_id not in guaranteed and not allow_conditional_sources:
                        add(
                            "conditional_source",
                            usage,
                            "该触发条件并非每次规则运行都会命中",
                        )
                        continue
                    param_defs = {
                        param.get("name"): param
                        for param in triggers_meta
                        .get(leaf.get("type", ""), {})
                        .get("params", [])
                        if isinstance(param, dict)
                    }
                    if not path or path[0] not in param_defs:
                        source_output = None
                    else:
                        param_def = param_defs[path[0]]
                        source_output = {
                            "name": path[0],
                            "type": _target_type(param_def),
                        }
                elif scope == "event":
                    candidates = []
                    for leaf in leaves:
                        output = output_for(
                            triggers_meta.get(leaf.get("type", ""), {}),
                            reference,
                        )
                        candidates.append(output)
                    if not candidates or any(output is None for output in candidates):
                        add(
                            "unknown_output",
                            usage,
                            "并非所有可能触发本规则的事件都提供该字段",
                        )
                        continue
                    source_types = {_source_type(output) for output in candidates}
                    if len(source_types) != 1:
                        add(
                            "binding_type_mismatch",
                            usage,
                            "不同触发分支对该字段声明了不同类型",
                        )
                        continue
                    source_output = candidates[0]
                elif scope == "step":
                    if not allow_steps:
                        add("step_not_available", usage, "开始前确认不能引用动作结果")
                        continue
                    node_id = reference.get("node")
                    source = actions_by_id.get(node_id)
                    if source is None:
                        add("unknown_source", usage, "引用的动作步骤不存在")
                        continue
                    source_index, source_action = source
                    if source_index >= item_index:
                        add("forward_reference", usage, "只能引用当前动作之前的步骤")
                        continue
                    source_output = output_for(
                        actions_meta.get(source_action.get("type", ""), {}),
                        reference,
                    )
                else:
                    add("invalid_reference", usage, f"未知的数据源 scope: {scope!r}")
                    continue

                if path and source_output is None:
                    add("unknown_output", usage, f"数据源未声明输出字段 {path[0]!r}")
                    continue
                if source_output and source_output.get("required") is False:
                    add("optional_output", usage, "该输出字段可能不存在")
                    continue
                source_type = _source_type(source_output)
                target_type = _target_type(target_params.get(param_name))
                if not _types_compatible(source_type, target_type):
                    add(
                        "binding_type_mismatch",
                        usage,
                        f"{source_type} 数据不能绑定到 {target_type} 参数",
                    )

    for index, item in enumerate(rule.get("preconditions", [])):
        if isinstance(item, dict):
            validate_item(
                item,
                item_index=index,
                field="preconditions",
                allow_steps=False,
                allow_conditional_sources=False,
            )
    for index, action in enumerate(actions):
        validate_item(
            action,
            item_index=index,
            field="actions",
            allow_steps=True,
            allow_conditional_sources=True,
        )
    return issues


def validate_rules(
    rules: List[Dict[str, Any]],
    triggers_meta: Dict[str, Dict[str, Any]],
    actions_meta: Dict[str, Dict[str, Any]],
) -> Tuple[int, int, List[Tuple[str, str]], List[Tuple[str, str]]]:
    """校验所有规则的插件引用和参数并返回统计结果"""
    issues: List[Tuple[str, str]] = []
    warnings: List[Tuple[str, str]] = []
    valid_count = 0

    for i, rule in enumerate(rules):
        rule_name = rule.get("name", f"规则 #{i+1}")

        condition_errors = validate_condition_tree(get_rule_condition(rule))
        if condition_errors:
            issues.extend((rule_name, error) for error in condition_errors)
            continue
        rule_events = get_rule_events(rule)
        all_events_valid = True
        for event_def in rule_events:
            event_type = event_def.get("type", "")
            if event_type and event_type not in triggers_meta:
                issues.append((rule_name, f"引用了未加载的触发器: {event_type}"))
                all_events_valid = False
        if not all_events_valid:
            # 没有有效触发器时跳过 action 校验
            continue

        rule_ok = True
        for j, action in enumerate(rule.get("actions", [])):
            action_type = action.get("type", "")
            if not action_type:
                issues.append((rule_name, f"actions[{j}] 缺少 type"))
                rule_ok = False
                continue

            if action_type not in actions_meta:
                issues.append((rule_name, f"引用了未加载的 action: {action_type}"))
                rule_ok = False
                continue

            # 只检查 schema 定义的参数类型
            action_meta = actions_meta[action_type]
            schema_params = action_meta.get("params", [])
            schema_param_names = {p["name"]: p for p in schema_params}
            rule_params = action.get("params", {})

            for param_name, param_value in rule_params.items():
                if param_name not in schema_param_names:
                    # 规则里出现 schema 没有的参数
                    hint = ""
                    if schema_param_names:
                        hint = f"（可用参数: {', '.join(sorted(schema_param_names))}）"
                    warnings.append((
                        rule_name,
                        f'action "{action_type}" 使用了未知参数: \'{param_name}\' {hint}',
                    ))
                    continue

                schema = schema_param_names[param_name]
                expected_type = schema.get("type", "string")
                if is_reference(param_value):
                    # 绑定类型由 validate_rule_bindings 检查
                    continue

                if expected_type == "number":
                    if not isinstance(param_value, (int, float)):
                        warnings.append((
                            rule_name,
                            f'action "{action_type}" 参数 \'{param_name}\' '
                            f'应为数字，实际: {type(param_value).__name__}',
                        ))
                elif expected_type == "bool":
                    if not isinstance(param_value, bool):
                        warnings.append((
                            rule_name,
                            f'action "{action_type}" 参数 \'{param_name}\' '
                            f'应为布尔值，实际: {type(param_value).__name__}',
                        ))
                elif expected_type == "select":
                    raw_options = schema.get("options", [])
                    # 同时接受包含 value 和 label 的对象以及旧版字符串
                    opt_values = [
                        o["value"] if isinstance(o, dict) else o
                        for o in raw_options
                    ]
                    if opt_values and param_value not in opt_values:
                        warnings.append((
                            rule_name,
                            f'action "{action_type}" 参数 \'{param_name}\' '
                            f'值 \'{param_value}\' 不在可选项中 '
                            f"({', '.join(map(str, opt_values))})",
                        ))
                # string 参数保留原值，不做严格类型检查

        if rule_ok:
            valid_count += 1

    return valid_count, len(rules), issues, warnings
