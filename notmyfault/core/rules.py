"""规则结构、插件参数和数据引用校验"""
import copy
import math
from typing import Any, Dict, List, Tuple

from notmyfault.core.bindings import (
    contains_dynamic_value,
    is_literal,
    is_reference,
)
from notmyfault.core.data_types import DataTypeError
from notmyfault.core.variables import variable_definitions
from notmyfault.extensions.protocol import owned_value_identity
from notmyfault.core.predicates import validate_predicate
from notmyfault.core.rule_model import normalize_rule_shape, normalize_rules, _BINDING_ID_RE, _RULE_ID_RE
from notmyfault.core.condition_runtime import ConditionRuntime
from notmyfault.core.conditions import (
    get_rule_condition, _is_event_leaf, _condition_children, iter_condition_events,
    get_rule_events, validate_condition_tree, check_event_params,
    config_fingerprint, _condition_op, _absence_nodes, _positive_events,
    _event_key, _canonical_config_value,
)




def iter_action_nodes(actions: Any, path: str = "actions", *, include_invalid: bool = False):
    if not isinstance(actions, list):
        return
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            if include_invalid:
                yield action, f"{path}[{index}]"
            continue
        location = f"{path}[{index}]"
        yield action, location
        for field in ("then", "else", "failure_actions"):
            yield from iter_action_nodes(action.get(field), f"{location}.{field}", include_invalid=include_invalid)


def get_rule_admin_plugins(
    rule: Dict[str, Any],
    triggers_meta: Dict[str, Dict[str, Any]],
    actions_meta: Dict[str, Dict[str, Any]],
) -> List[str]:
    """返回规则引用的管理员插件，包含失败后动作。"""
    required = set()
    for event in get_rule_events(rule):
        plugin_id = event.get("type")
        if "admin" in (triggers_meta.get(plugin_id, {}).get("permissions") or []):
            required.add(plugin_id)

    for item, _location in iter_action_nodes(rule.get("actions"), "actions"):
        plugin_id = item.get("type")
        if "admin" in (actions_meta.get(plugin_id, {}).get("permissions") or []):
            required.add(plugin_id)
    return sorted(required)


def validate_rule_structure(rule: Any) -> List[str]:
    """校验规则结构是否符合引擎输入格式"""
    errors: List[str] = []
    if not isinstance(rule, dict):
        return ["规则必须是对象"]
    try:
        rule = normalize_rule_shape(rule)
    except ValueError as error:
        return [str(error)]
    try:
        variable_definitions(rule)
    except DataTypeError as error:
        errors.append(str(error))

    if "rule_id" in rule and (
        not isinstance(rule["rule_id"], str)
        or not _RULE_ID_RE.fullmatch(rule["rule_id"])
    ):
        errors.append("rule_id 无效")

    if not isinstance(rule.get("name"), str) or not rule["name"].strip():
        errors.append("name 必须是非空字符串")

    if "concurrency" in rule:
        concurrency = rule["concurrency"]
        if not isinstance(concurrency, dict):
            errors.append("concurrency 必须是对象")
        else:
            mode = concurrency.get("mode", "parallel")
            if mode not in ("single", "queue", "replace", "parallel"):
                errors.append(
                    "concurrency.mode 必须是 single / queue / replace / parallel"
                )
            for field, ceiling in (
                ("max_concurrency", 32),
                ("queue_limit", 1000),
            ):
                if field not in concurrency:
                    continue
                value = concurrency[field]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 1 <= value <= ceiling
                ):
                    errors.append(
                        f"concurrency.{field} 必须是 1 到 {ceiling} 的整数"
                    )

    if "condition" not in rule:
        errors.append("必须配置 condition")
    else:
        errors.extend(validate_condition_tree(get_rule_condition(rule)))

    def validate_action(action: Any, path: str, *, allow_failure_actions: bool) -> None:
        if not isinstance(action, dict):
            errors.append(f"{path} 必须是对象")
            return
        if action.get("type") == "set_variable":
            if not isinstance(action.get("variable"), str):
                errors.append(f"{path}.variable 必须是运行变量 id")
            if "value" not in action:
                errors.append(f"{path}.value 不能为空")
            if action.get("on_error", "stop") not in ("stop", "continue"):
                errors.append(f"{path}.on_error 必须是 stop 或 continue")
            for field in ("params", "retry", "retry_delay_seconds", "retry_backoff", "timeout_seconds", "failure_actions"):
                if field in action:
                    errors.append(f"{path} 的赋值动作不支持 {field}")
            return
        if action.get("type") == "if":
            if action.get("on_error", "stop") not in ("stop", "continue"):
                errors.append(f"{path}.on_error 必须是 stop 或 continue")
            errors.extend(validate_predicate(action.get("condition"), f"{path}.condition"))
            for branch in ("then", "else"):
                items = action.get(branch, [])
                if not isinstance(items, list):
                    errors.append(f"{path}.{branch} 必须是动作列表")
                    continue
                for index, item in enumerate(items):
                    validate_action(item, f"{path}.{branch}[{index}]", allow_failure_actions=allow_failure_actions)
            if not action.get("then") and not action.get("else"):
                errors.append(f"{path} 至少需要一个分支动作")
            for field in ("params", "retry", "retry_delay_seconds", "retry_backoff", "timeout_seconds", "failure_actions"):
                if field in action:
                    errors.append(f"{path} 的 if 不支持 {field}")
            return
        if not str(action.get("type", "")).strip():
            errors.append(f"{path}.type 不能为空")
        if "params" in action and not isinstance(action["params"], dict):
            errors.append(f"{path}.params 必须是对象")
        on_error = action.get("on_error", "stop")
        if not isinstance(on_error, str) or on_error not in {"stop", "continue"}:
            errors.append(f"{path}.on_error 必须是 stop 或 continue")
        if "retry" in action:
            retry = action["retry"]
            valid_integer = (
                not isinstance(retry, bool)
                and (
                    isinstance(retry, int)
                    or isinstance(retry, float) and retry.is_integer()
                    or isinstance(retry, str) and retry.strip().isdigit()
                )
            )
            if not valid_integer or not 0 <= int(retry) <= 3:
                errors.append(f"{path}.retry 必须是 0 到 3 的整数")
        if "retry_delay_seconds" in action:
            delay = action["retry_delay_seconds"]
            try:
                delay_number = float(delay)
            except (TypeError, ValueError):
                delay_number = -1
            if (
                isinstance(delay, bool)
                or not math.isfinite(delay_number)
                or not 0 <= delay_number <= 3600
            ):
                errors.append(
                    f"{path}.retry_delay_seconds 必须是 0 到 3600 的数字"
                )
        if "timeout_seconds" in action:
            timeout = action["timeout_seconds"]
            try:
                timeout_number = float(timeout)
            except (TypeError, ValueError):
                timeout_number = 0
            if (
                isinstance(timeout, bool)
                or not math.isfinite(timeout_number)
                or not 1 <= timeout_number <= 86400
            ):
                errors.append(
                    f"{path}.timeout_seconds 必须是 1 到 86400 的数字"
                )
        retry_backoff = action.get("retry_backoff", "fixed")
        if not isinstance(retry_backoff, str) or retry_backoff not in {
            "fixed",
            "exponential",
        }:
            errors.append(
                f"{path}.retry_backoff 必须是 fixed 或 exponential"
            )
        if "failure_actions" not in action:
            return
        failure_actions = action["failure_actions"]
        if not allow_failure_actions:
            errors.append(f"{path}.failure_actions 不允许继续嵌套")
        elif not isinstance(failure_actions, list) or not failure_actions:
            errors.append(f"{path}.failure_actions 至少需要一个动作")
        else:
            for failure_index, failure_action in enumerate(failure_actions):
                validate_action(
                    failure_action,
                    f"{path}.failure_actions[{failure_index}]",
                    allow_failure_actions=False,
                )

    actions = rule.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("actions 至少需要一个动作")
    else:
        for index, action in enumerate(actions):
            validate_action(action, f"actions[{index}]", allow_failure_actions=True)

    if rule.get("preconditions"):
        errors.append("运行前检查已移除，请改用 NOT 触发条件或 if 分支")

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


def validate_rule_binding_ids(rule: Dict[str, Any], *, allow_missing: bool = False) -> List[str]:
    """校验 v2 节点身份，v1 规则的补齐由配置迁移器完成"""
    errors: List[str] = []
    try:
        rule = normalize_rule_shape(rule)
    except ValueError as error:
        return [str(error)]
    seen: set[str] = set()

    def check(node: Dict[str, Any], prefix: str, path: str) -> None:
        if allow_missing and "binding_id" not in node:
            return
        _validate_node_binding_id(node, prefix, path, seen, errors)

    def visit(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        if _is_event_leaf(node):
            check(node, "t", path)
            return
        for index, child in enumerate(_condition_children(node)):
            visit(child, f"{path}.children[{index}]")

    condition = get_rule_condition(rule)
    if condition is not None:
        visit(condition, "condition")
    for item, location in iter_action_nodes(rule.get("actions"), "actions"):
        check(item, "a", location)
    return errors


def validate_rules_structure(rules: Any) -> List[str]:
    """校验规则列表并返回带索引的错误供写入入口复用"""
    if not isinstance(rules, list):
        return ["rules 必须是列表"]
    errors: List[str] = []
    seen_rule_ids: set[str] = set()
    for index, rule in enumerate(rules):
        name = rule.get("name") if isinstance(rule, dict) else None
        label = str(name).strip() if name else f"#{index + 1}"
        errors.extend(
            f"规则 {label}: {error}"
            for error in (
                validate_rule_structure(rule)
                + (
                    validate_rule_binding_ids(rule, allow_missing=True)
                    if isinstance(rule, dict)
                    else []
                )
            )
        )
        if isinstance(rule, dict) and isinstance(rule.get("rule_id"), str):
            rule_id = rule["rule_id"]
            if rule_id in seen_rule_ids:
                errors.append(f"规则 {label}: rule_id 与其他规则重复")
            seen_rule_ids.add(rule_id)
    return errors


class RuleStructureError(ValueError):
    def __init__(self, errors: List[str]) -> None:
        self.errors = errors
        super().__init__("；".join(errors))


def normalize_rule_input(rules: Any) -> List[Dict[str, Any]]:
    try:
        shaped = [normalize_rule_shape(rule) if isinstance(rule, dict) else rule for rule in rules] if isinstance(rules, list) else rules
        errors = validate_rules_structure(shaped)
        if errors:
            raise RuleStructureError(errors)
        normalized = normalize_rules(shaped)
        errors = validate_rules_structure(normalized)
        if errors:
            raise RuleStructureError(errors)
        return normalized
    except RuleStructureError:
        raise
    except ValueError as error:
        raise RuleStructureError([str(error)]) from error


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
    if _condition_op(node) == "not":
        return set()
    if _condition_op(node) == "all":
        return set().union(*child_sets)
    result = set(child_sets[0])
    for child_set in child_sets[1:]:
        result.intersection_update(child_set)
    return result


def _valid_plugin_data(
    value: Any,
    plugin_meta: Dict[str, Any],
    param: Dict[str, Any],
) -> bool:
    identity = owned_value_identity(value)
    if identity is None:
        if isinstance(value, dict) and "$type" in value:
            return False
        if not isinstance(value, dict) or not value:
            return False
        contributes = plugin_meta.get("contributes")
        editors = (
            contributes.get("parameter_editors", [])
            if isinstance(contributes, dict) else []
        )
        return any(
            isinstance(editor, dict)
            and editor.get("parameter") == param.get("name")
            and editor.get("accepts_legacy") is True
            for editor in editors
        )
    package_name, data_type_id, version = identity
    if package_name != plugin_meta.get("package_name"):
        return False
    if data_type_id != param.get("data_type"):
        return False
    contributes = plugin_meta.get("contributes")
    data_types = contributes.get("data_types", []) if isinstance(contributes, dict) else []
    declared = next(
        (
            item for item in data_types
            if isinstance(item, dict) and item.get("id") == data_type_id
        ),
        None,
    )
    return isinstance(declared, dict) and declared.get("version") == version


def validate_rule_bindings(
    rule: Dict[str, Any],
    triggers_meta: Dict[str, Dict[str, Any]],
    actions_meta: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    from notmyfault.core.binding_schema import check_rule_bindings

    return check_rule_bindings(normalize_rule_shape(rule), triggers_meta, actions_meta)


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
        try:
            rule = normalize_rule_shape(rule)
        except ValueError as error:
            issues.append((rule_name, str(error)))
            continue

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
                continue
            trigger_meta = triggers_meta.get(event_type, {})
            event_params = event_def.get("params", {})
            if not isinstance(event_params, dict):
                continue
            for schema in trigger_meta.get("params", []):
                if not isinstance(schema, dict):
                    continue
                param_name = schema.get("name", "")
                if schema.get("required") is True and param_name not in event_params:
                    issues.append((
                        rule_name,
                        f'trigger "{event_type}" 缺少必填参数: {param_name}',
                    ))
                    all_events_valid = False
                    continue
                if param_name not in event_params:
                    continue
                if schema.get("type") != "plugin_data":
                    continue
                if is_reference(event_params[param_name]):
                    issues.append((rule_name, f'trigger "{event_type}" 参数 {param_name!r} 不支持数据引用'))
                    all_events_valid = False
                    continue
                if not _valid_plugin_data(
                    event_params[param_name], trigger_meta, schema
                ):
                    issues.append((
                        rule_name,
                        f'trigger "{event_type}" 参数 \'{param_name}\' '
                        "不是该插件声明的数据，请重新编辑",
                    ))
                    all_events_valid = False
        if not all_events_valid:
            # 没有有效触发器时跳过 action 校验
            continue

        rule_ok = True
        for action, action_path in iter_action_nodes(rule.get("actions", []), include_invalid=True):
            if not isinstance(action, dict):
                issues.append((rule_name, f"{action_path} 必须是对象"))
                rule_ok = False
                continue
            if action.get("type") in ("if", "set_variable"):
                continue
            action_type = action.get("type", "")
            if not action_type:
                issues.append((rule_name, f"{action_path} 缺少 type"))
                rule_ok = False
                continue

            if action_type not in actions_meta:
                issues.append((rule_name, f"引用了未加载的 action: {action_type}"))
                rule_ok = False
                continue

            if (
                action.get("timeout_seconds") is not None
                and actions_meta[action_type].get("cancellation_api") != "runtime-v1"
            ):
                issues.append((
                    rule_name,
                    f'{action_path} 的 action "{action_type}" 不支持安全取消，不能设置运行超时',
                ))
                rule_ok = False
                continue

            # 只检查 schema 定义的参数类型
            action_meta = actions_meta[action_type]
            schema_params = action_meta.get("params", [])
            schema_param_names = {p["name"]: p for p in schema_params}
            rule_params = action.get("params", {})

            for schema in schema_params:
                if schema.get("required") is True and schema["name"] not in rule_params:
                    issues.append((
                        rule_name,
                        f'action "{action_type}" 缺少必填参数: {schema["name"]}',
                    ))
                    rule_ok = False

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
                if schema.get("type") != "plugin_data":
                    continue
                if is_literal(param_value):
                    param_value = param_value["$literal"]
                elif contains_dynamic_value(param_value):
                    continue
                if not _valid_plugin_data(param_value, action_meta, schema):
                    issues.append((
                        rule_name,
                        f'action "{action_type}" 参数 {param_name!r} '
                        "不是该插件声明的数据，请重新编辑",
                    ))
                    rule_ok = False

        binding_issues = validate_rule_bindings(
            rule,
            triggers_meta,
            actions_meta,
        )
        if binding_issues:
            issues.extend(
                (rule_name, issue.get("message", "规则数据绑定无效"))
                for issue in binding_issues
            )
            rule_ok = False

        if rule_ok:
            valid_count += 1

    return valid_count, len(rules), issues, warnings
