"""规则引擎的核心纯函数：条件树、事件匹配、规则校验、触发器参数聚合。

全都是无 I/O、无状态的纯函数，随你怎么單测，不用起引擎、不用读文件、不用 mock。
engine.py 只管拿这些函数的返回值去打印和记诊断，逻辑和副作用分得干干净净。
"""
import json
import threading
import time
from typing import Any, Dict, Iterable, List, Tuple


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


# ---------------------------------------------------------------------------
# 参数匹配
# ---------------------------------------------------------------------------

def check_event_params(event_def: Dict[str, Any], event_payload: Dict[str, Any]) -> bool:
    """检查事件 payload 是否匹配事件定义的参数（允许 payload 有额外字段）。"""
    expected_params = event_def.get("params", {})
    for key, expected_val in expected_params.items():
        # 缺字段不能等同于满足条件，否则多个规则会互相误触发。
        if key not in event_payload or event_payload[key] != expected_val:
            return False
    return True


def match_rule(rule: Dict[str, Any], event_type: str, event_payload: Dict[str, Any]) -> bool:
    """判断单条规则是否匹配给定事件。

    只要规则里有一个 event_def 的 type 对上、参数也对上，就算匹配。
    """
    for event_def in get_rule_events(rule):
        if event_def.get("type") != event_type:
            continue
        if check_event_params(event_def, event_payload):
            return True
    return False


def _condition_op(node: Dict[str, Any]) -> str:
    """读取条件运算符，兼容 ``type: and/or`` 的早期格式。"""
    op = node.get("op", node.get("type", "any"))
    return {"or": "any", "and": "all"}.get(str(op).lower(), str(op).lower())


def _event_key(event_def: Dict[str, Any]) -> str:
    """事件叶子的稳定键；用于保存最近一次命中，而不是依赖对象 id。"""
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
        self._seen: Dict[str, Dict[str, tuple[float, Dict[str, Any]]]] = {}
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
                {**item, "payload": dict(item["payload"])}
                for item in self._last_matches.get(rule_key, [])
            ]

    def match(
        self,
        rule_key: str,
        rule: Dict[str, Any],
        event_type: str,
        event_payload: Dict[str, Any],
        now: float | None = None,
    ) -> bool:
        """记录入站事件后，判断规则条件是否形成一组新的有效命中。"""
        node = get_rule_condition(rule)
        if node is None:
            return False
        timestamp = time.monotonic() if now is None else now
        matching_leaves = [
            leaf for leaf in iter_condition_events(node)
            if leaf.get("type") == event_type and check_event_params(leaf, event_payload)
        ]
        if not matching_leaves:
            return False

        with self._lock:
            seen = self._seen.setdefault(rule_key, {})
            for leaf in matching_leaves:
                seen[_event_key(leaf)] = (timestamp, dict(event_payload))

            matched, signature = self._evaluate(node, seen)
            if not matched:
                return False
            # 同一批 AND 命中不能被后续无关事件或重复轮询反复执行。
            if self._fired.get(rule_key) == signature:
                return False
            self._fired[rule_key] = signature
            self._last_matches[rule_key] = [
                {
                    "event": json.loads(key),
                    "timestamp": fired_at,
                    "payload": dict(seen[key][1]),
                }
                for key, fired_at in signature
                if key in seen
            ]
            return True

    def _evaluate(
        self,
        node: Dict[str, Any],
        seen: Dict[str, tuple[float, Dict[str, Any]]],
    ) -> tuple[bool, tuple[tuple[str, float], ...]]:
        if _is_event_leaf(node):
            key = _event_key(node)
            entry = seen.get(key)
            return (entry is not None, ((key, entry[0]),) if entry else ())

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

    同一个 trigger 只需要开一条线程，所有规则参数一次性塞给它，
    省得大家各蹲各的点。
    """
    aggregated: Dict[str, List[Dict[str, Any]]] = {}
    for rule in rules:
        for event_def in get_rule_events(rule):
            event_type = event_def.get("type")
            if not event_type:
                continue
            aggregated.setdefault(event_type, []).append(event_def.get("params", {}))
    return aggregated


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
