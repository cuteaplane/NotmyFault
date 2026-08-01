"""规则引擎的核心纯函数：条件树、事件匹配、规则校验、触发器参数聚合。

全都是无 I/O、无状态的纯函数，随你怎么單测，不用起引擎、不用读文件、不用 mock。
engine.py 只管拿这些函数的返回值去打印和记诊断，逻辑和副作用分得干干净净。
"""
import json
import copy
import threading
import time
import re
from typing import Any, Dict, Iterable, List, Tuple

from notmyfault.core.bindings import is_reference, iter_references


_BINDING_ID_RE = re.compile(r"^[tap]_[a-z0-9_]{6,64}$")

# ---------------------------------------------------------------------------
# 事件提取
# ---------------------------------------------------------------------------

def get_rule_condition(rule: Dict[str, Any]) -> Dict[str, Any] | None:
    """返回规则的条件树，并兼容两代扁平规则格式。

    新格式使用 ``op``/``children``（``any``/``all`` 可嵌套）；旧 Dashboard
    写出的 ``{type: "or", events: [...]}`` 与 ``event`` 仍在这里归一化，
    所以升级不会让已有自动化失效。
    """
    condition = rule.get("condition")
    if isinstance(condition, dict):
        return condition
    event = rule.get("event") or rule.get("trigger")
    return event if isinstance(event, dict) else None


def _is_event_leaf(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("type"), str) and \
        "children" not in node and "events" not in node


def _condition_children(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取条件节点子项，兼容旧的 events 字段。"""
    children = node.get("children")
    if not isinstance(children, list):
        children = node.get("events", [])
    return [child for child in children if isinstance(child, dict)]


def iter_condition_events(node: Dict[str, Any] | None) -> Iterable[Dict[str, Any]]:
    """深度优先枚举条件树中的事件叶子。"""
    if not isinstance(node, dict):
        return
    if _is_event_leaf(node):
        yield node
        return
    for child in _condition_children(node):
        yield from iter_condition_events(child)


def get_rule_events(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从规则的任意条件树中提取所有事件条件。"""
    return list(iter_condition_events(get_rule_condition(rule)))


def validate_condition_tree(node: Dict[str, Any] | None) -> List[str]:
    """校验可序列化的条件树结构，拒绝引擎无法解释的运算符。"""
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
    """校验一条可由 Dashboard 保存并交给引擎执行的规则。"""
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
    """校验 v2 节点身份；配置迁移器负责给 v1 规则补齐。"""
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
    """校验规则列表并返回带索引的错误，供所有写入入口复用。"""
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


# ---------------------------------------------------------------------------
# 参数匹配
# ---------------------------------------------------------------------------

def check_event_params(event_def: Dict[str, Any], event_payload: Dict[str, Any]) -> bool:
    """检查事件 payload 是否匹配事件定义的参数（允许 payload 有额外字段）。

    v1/legacy 触发器语义：事件叶子的 params 是过滤条件，payload 必须包含
    相同键值才命中。event-v2 触发器不走本函数，改用配置匹配。
    """
    expected_params = event_def.get("params", {})
    for key, expected_val in expected_params.items():
        # 缺字段不能等同于满足条件，否则多个规则会互相误触发。
        if key not in event_payload or event_payload[key] != expected_val:
            return False
    return True


def _canonical_config_value(value: Any) -> Any:
    """递归规范化配置值，消除"写法不同但语义相同"的指纹差异。

    整数值 90 与 90.0 归一为 90（否则同一配置因写法不同会去重失败或
    热重载后不命中）；bool 保持独立，避免与 0/1 混为一谈。
    """
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
    """event-v2 触发器实例配置的稳定指纹。

    v2 语义下事件叶子的 params 就是该实例的配置；配置匹配 = 事件携带的
    实例指纹与叶子配置指纹相等，payload 不再参与命中判断。指纹前先做
    规范化，让 90/90.0 等写法差异不影响匹配与去重。
    """
    if not isinstance(params, dict):
        params = {}
    return json.dumps(
        _canonical_config_value(params),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _condition_op(node: Dict[str, Any]) -> str:
    """读取条件运算符，兼容 ``type: and/or`` 的早期格式。"""
    op = node.get("op", node.get("type", "any"))
    return {"or": "any", "and": "all"}.get(str(op).lower(), str(op).lower())


def _event_key(event_def: Dict[str, Any]) -> str:
    """事件叶子的稳定键；用于保存最近一次命中，而不是依赖对象 id。"""
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
    """有状态地评估嵌套条件树。

    单一事件与 OR 立即触发。AND 则保存每个事件叶子的最近一次命中，并可在
    节点上声明 ``within_seconds``，例如 ``{op: "all", within_seconds: 300}``
    表示所有子条件必须在五分钟内发生。每一组命中只触发一次，直到其中任一
    事件再次发生并形成新的组合。
    """

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
        """返回最近一次成功条件组合的全部事件，供执行流水线消费。"""
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
        """记录入站事件后，判断规则条件是否形成一组新的有效命中。

        ``instance`` 由 event-v2 触发器传入（携带 config 指纹）：命中判定
        改用配置匹配——叶子 params（即该实例的配置）与指纹相等即命中，
        payload 不再参与；v1/legacy 触发器保持 payload 过滤语义。
        """
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
            # 同一批 AND 命中不能被后续无关事件或重复轮询反复执行。
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

        # 默认 any，且保留旧 condition.type == "or" 的行为。选择最近一次
        # 命中的分支，不能总拿第一个缓存分支，否则 OR 的第二个事件会被误判
        # 成与上一次相同的组合。
        matches = [signature for ok, signature in states if ok]
        if matches:
            latest = max(
                matches,
                key=lambda signature: max((item[1] for item in signature), default=float("-inf")),
            )
            return True, latest
        return False, ()


# ---------------------------------------------------------------------------
# 触发器参数聚合
# ---------------------------------------------------------------------------

def aggregate_trigger_params(rules: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """把所有规则里同一个 trigger 的参数打包成列表。

    event-v1 同一个 trigger 只开一条线程；event-v2 每配置一个隔离实例
    （相同配置由 TriggerSupervisor 按指纹去重）。深拷贝避免线程共享可变字典。
    省得各规则各蹲各的点。
    """
    aggregated: Dict[str, List[Dict[str, Any]]] = {}
    for rule in rules:
        for event_def in get_rule_events(rule):
            event_type = event_def.get("type")
            if not event_type:
                continue
            aggregated.setdefault(event_type, []).append(copy.deepcopy(event_def.get("params", {})))
    return aggregated


# ---------------------------------------------------------------------------
# 运行数据绑定静态校验
# ---------------------------------------------------------------------------

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
    """返回条件树每次成立时都必然出现的触发器节点。"""
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
    """静态验证结构化 ``$ref`` 的来源、顺序、可用性和类型。"""
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


# ---------------------------------------------------------------------------
# 规则校验（从 engine._validate_all_rules 下沉）
# ---------------------------------------------------------------------------

def validate_rules(
    rules: List[Dict[str, Any]],
    triggers_meta: Dict[str, Dict[str, Any]],
    actions_meta: Dict[str, Dict[str, Any]],
) -> Tuple[int, int, List[Tuple[str, str]], List[Tuple[str, str]]]:
    """校验所有规则的 event/action 引用和参数是否与已加载插件匹配。

    非致命：只收集问题，不拒绝任何规则。

    Returns:
        (valid_count, total_count, issues, warnings)
        - issues: [(rule_name, message)] 引用类问题——触发器/action 没装上，
          需要记录到诊断里让 Dashboard 能看到。
        - warnings: [(rule_name, message)] 参数类型不匹配——只是提醒用户
          配置可能写错了，只打印不进诊断（保持原行为）。
    """
    issues: List[Tuple[str, str]] = []
    warnings: List[Tuple[str, str]] = []
    valid_count = 0

    for i, rule in enumerate(rules):
        rule_name = rule.get("name", f"规则 #{i+1}")

        # --- event 引用检查 ---
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
            continue  # 连触发器都没装上，后面的 action 校验也没意义了

        # --- action 引用 + 参数检查 ---
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

            # 参数类型校验——只在 schema 里定义了的参数才查类型
            action_meta = actions_meta[action_type]
            schema_params = action_meta.get("params", [])
            schema_param_names = {p["name"]: p for p in schema_params}
            rule_params = action.get("params", {})

            for param_name, param_value in rule_params.items():
                if param_name not in schema_param_names:
                    # 未知参数：你写了个 schema 里没有的参数名
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
                    # 结构化绑定由 validate_rule_bindings 按来源输出类型检查。
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
                    # 兼容新格式 {"value","label"} 和旧格式字符串
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
                # string 类型不做严格检查——用户爱填什么填什么

        if rule_ok:
            valid_count += 1

    return valid_count, len(rules), issues, warnings
