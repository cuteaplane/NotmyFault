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
from notmyfault.core.workflow_graph import (
    NATIVE_NODES, iter_rule_actions, get_all_rule_events, iter_rule_event_nodes, validate_workflow_graph, entry_event_group,
)
from notmyfault.core.rule_model import normalize_rule_shape, normalize_rules, _BINDING_ID_RE, _RULE_ID_RE
from notmyfault.core.condition_runtime import ConditionRuntime
from notmyfault.core.conditions import check_event_params, config_fingerprint, _canonical_config_value





def get_rule_admin_plugins(
    rule: Dict[str, Any],
    triggers_meta: Dict[str, Dict[str, Any]],
    actions_meta: Dict[str, Dict[str, Any]],
) -> List[str]:
    """返回规则引用的管理员插件，包含失败后动作。"""
    required = set()
    for event in get_all_rule_events(rule):
        plugin_id = event.get("type")
        if "admin" in (triggers_meta.get(plugin_id, {}).get("permissions") or []):
            required.add(plugin_id)

    for item, _location in iter_rule_actions(rule):
        plugin_id = item.get("type")
        if "admin" in (actions_meta.get(plugin_id, {}).get("permissions") or []):
            required.add(plugin_id)
    return sorted(required)


_RULE_TOP_LEVEL_FIELDS = frozenset({'rule_id', 'name', 'folder', 'workflow', 'constants', 'variables', 'concurrency'})


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

    unknown = sorted(set(rule) - _RULE_TOP_LEVEL_FIELDS)
    if unknown:
        errors.append('包含未知字段: ' + ', '.join(unknown))

    if "concurrency" in rule:
        concurrency = rule["concurrency"]
        if not isinstance(concurrency, dict):
            errors.append("concurrency 必须是对象")
        else:
            mode = concurrency.get("mode", "single")
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

    from notmyfault.core.canvas_workflow import validate_canvas_graph
    errors.extend(validate_canvas_graph(rule.get('workflow')))

    def validate_action(action: Any, path: str) -> None:
        if not isinstance(action, dict):
            errors.append(f"{path} 必须是对象")
            return
        if action.get("type") in ("trigger", "condition", "logic", "event_group", "action_group", "merge", "end"):
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
            try:
                valid_retry = valid_integer and 0 <= int(retry) <= 3
            except (ValueError, OverflowError):
                valid_retry = False
            if not valid_retry:
                errors.append(f"{path}.retry 必须是 0 到 3 的整数")
        if "retry_delay_seconds" in action:
            delay = action["retry_delay_seconds"]
            try:
                delay_number = float(delay)
            except (TypeError, ValueError, OverflowError):
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
            except (TypeError, ValueError, OverflowError):
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
    for node, path in iter_rule_actions(rule):
        validate_action(node, path)

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

    if "workflow" in rule:
        trigger_ids = {}
        for event in iter_rule_event_nodes(rule):
            identity = event.get("binding_id")
            if not isinstance(identity, str):
                check(event, "t", "workflow.triggers")
                continue
            if identity not in trigger_ids:
                if identity not in seen:
                    check(event, "t", "workflow.triggers")
                trigger_ids[identity] = event
            elif event != trigger_ids[identity]:
                errors.append("同一触发器 binding_id 的配置不一致")
        for item, location in iter_rule_actions(rule):
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


def validate_automation_references(rule, known_rule_ids):
    errors = []

    def condition(node, path):
        if not isinstance(node, dict):
            return
        if node.get("op") == "run_status" and (
            not isinstance(node.get("rule_id"), str) or node["rule_id"] not in known_rule_ids
        ):
            errors.append(f"{path}.rule_id 引用的自动化不存在")


    for action, path in iter_rule_actions(rule):
        if action.get('type') == 'condition':
            condition(action.get('predicate'), f'{path}.predicate')
    return errors


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
        known_rule_ids = {rule["rule_id"] for rule in normalized}
        reference_errors = [
            f"规则 {rule['name']}: {error}" for rule in normalized
            for error in validate_automation_references(rule, known_rule_ids)
        ]
        if reference_errors:
            raise RuleStructureError(reference_errors)
        return normalized
    except RuleStructureError:
        raise
    except ValueError as error:
        raise RuleStructureError([str(error)]) from error


def aggregate_trigger_params(rules: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """把同一 trigger 的参数聚合成列表，并复制字典隔离规则配置"""
    aggregated: Dict[str, List[Dict[str, Any]]] = {}
    for rule in rules:
        from notmyfault.core.canvas_workflow import CanvasProgram
        program = CanvasProgram(rule)
        active_triggers = {program.nodes[identity]["trigger"]["binding_id"] for identity in program.active_nodes() if program.nodes[identity]["type"] == "trigger"}
        for event_def in get_all_rule_events(rule):
            if event_def["binding_id"] not in active_triggers:
                continue
            event_type = event_def.get("type")
            if not event_type:
                continue
            aggregated.setdefault(event_type, []).append(copy.deepcopy(event_def.get("params", {})))
    return aggregated


def _valid_plugin_data(
    value: Any,
    plugin_meta: Dict[str, Any],
    param: Dict[str, Any],
) -> bool:
    identity = owned_value_identity(value)
    if identity is None:
        return False
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

    rule = normalize_rule_shape(rule)
    issues = check_rule_bindings(rule, triggers_meta, actions_meta)
    locations = {path: node['binding_id'] for node, path in iter_rule_actions(rule)}
    for issue in issues:
        for path, identity in locations.items():
            if issue.get('location', '').startswith(path + '.') or issue.get('location') == path:
                issue['node_id'] = identity
                break
    return issues


def validate_rules(
    rules: List[Dict[str, Any]],
    triggers_meta: Dict[str, Dict[str, Any]],
    actions_meta: Dict[str, Dict[str, Any]],
    *, known_rule_ids=None,
) -> Tuple[int, int, List[Tuple[str, str]], List[Tuple[str, str]]]:
    """校验所有规则的插件引用和参数并返回统计结果"""
    issues: List[Tuple[str, str]] = []
    warnings: List[Tuple[str, str]] = []
    valid_count = 0
    if known_rule_ids is None:
        known_rule_ids = {rule.get("rule_id") for rule in rules if isinstance(rule, dict)}

    for i, rule in enumerate(rules):
        rule_name = rule.get("name", f"规则 #{i+1}")
        try:
            rule = normalize_rule_shape(rule)
        except ValueError as error:
            issues.append((rule_name, str(error)))
            continue

        condition_errors = validate_rule_structure(rule)
        condition_errors.extend(validate_automation_references(rule, known_rule_ids))
        if condition_errors:
            issues.extend((rule_name, error) for error in condition_errors)
            continue
        from notmyfault.core.canvas_workflow import CanvasProgram
        program = CanvasProgram(rule)
        active = program.active_nodes()
        active_triggers = {program.nodes[identity]["trigger"]["binding_id"] for identity in active if program.nodes[identity]["type"] == "trigger"}
        rule_events = [event for event in get_all_rule_events(rule) if event["binding_id"] in active_triggers]
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
                # 必填参数只写空白等于没写，触发器启动时会报错退出
                if (
                    schema.get("required") is True
                    and isinstance(event_params[param_name], str)
                    and not event_params[param_name].strip()
                ):
                    issues.append((
                        rule_name,
                        f'trigger "{event_type}" 必填参数不能为空: {param_name}',
                    ))
                    all_events_valid = False
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
        for action, action_path in iter_rule_actions(rule, include_invalid=True):
            if isinstance(action, dict) and action["binding_id"] not in active:
                continue
            if not isinstance(action, dict):
                issues.append((rule_name, f"{action_path} 必须是对象"))
                rule_ok = False
                continue
            if action.get("type") in NATIVE_NODES:
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

        from notmyfault.core.binding_schema import check_rule_bindings
        binding_issues = check_rule_bindings(rule, triggers_meta, actions_meta)
        if binding_issues:
            issues.extend(
                (rule_name, issue.get("message", "规则数据绑定无效"))
                for issue in binding_issues
            )
            rule_ok = False

        if rule_ok:
            valid_count += 1

    return valid_count, len(rules), issues, warnings
