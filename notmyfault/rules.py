"""规则引擎的核心纯函数：事件提取、参数匹配、规则校验、触发器参数聚合。

全都是无 I/O、无状态的纯函数，随你怎么單测，不用起引擎、不用读文件、不用 mock。
engine.py 只管拿这些函数的返回值去打印和记诊断，逻辑和副作用分得干干净净。
"""
import sys
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# 事件提取
# ---------------------------------------------------------------------------

def get_rule_events(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从规则中提取所有事件条件。

    新格式: rule.condition.events -> event 列表
    旧格式: rule.event -> 单个事件包装为列表
    """
    condition = rule.get("condition")
    if condition is not None and isinstance(condition, dict):
        events = condition.get("events", [])
        if isinstance(events, list) and events:
            return events
    event = rule.get("event") or rule.get("trigger")
    if event and isinstance(event, dict):
        return [event]
    return []


# ---------------------------------------------------------------------------
# 参数匹配
# ---------------------------------------------------------------------------

def check_event_params(event_def: Dict[str, Any], event_payload: Dict[str, Any]) -> bool:
    """检查事件 payload 是否匹配事件定义的参数（事件可含额外参数）。"""
    expected_params = event_def.get("params", {})
    for key, expected_val in expected_params.items():
        if event_payload.get(key) != expected_val:
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
                    options = schema.get("options", [])
                    if options and param_value not in options:
                        warnings.append((
                            rule_name,
                            f'action "{action_type}" 参数 \'{param_name}\' '
                            f'值 \'{param_value}\' 不在可选项中 '
                            f"({', '.join(map(str, options))})",
                        ))
                # string 类型不做严格检查——用户爱填什么填什么

        if rule_ok:
            valid_count += 1

    return valid_count, len(rules), issues, warnings
