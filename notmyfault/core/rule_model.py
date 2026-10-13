from __future__ import annotations

import copy
import re
import secrets
from typing import Any, Dict, List

_BINDING_ID_RE = re.compile(r'^[ta]_[a-z0-9_]{6,64}$')
_RULE_ID_RE = re.compile(r'^r_[a-z0-9_]{6,64}$')


def _new_binding_id(prefix: str) -> str:
    return f'{prefix}_{secrets.token_hex(6)}'


def ensure_rule_id(rule: Dict[str, Any], seen: set[str] | None = None) -> Dict[str, Any]:
    copied = copy.deepcopy(rule)
    used = seen if seen is not None else set()
    identity = copied.get('rule_id')
    if identity is None:
        identity = f'r_{secrets.token_hex(6)}'
        while identity in used:
            identity = f'r_{secrets.token_hex(6)}'
        copied['rule_id'] = identity
    elif not isinstance(identity, str) or not _RULE_ID_RE.fullmatch(identity) or identity in used:
        raise ValueError('rule_id 无效或重复')
    used.add(identity)
    return copied


def ensure_rule_binding_ids(rule: Dict[str, Any]) -> Dict[str, Any]:
    copied = copy.deepcopy(rule)
    def action_identity(node):
        node.setdefault('binding_id', _new_binding_id('a'))
        for failure in node.get('failure_actions', []):
            action_identity(failure)
    for node in copied.get('workflow', {}).get('nodes', []):
        action_identity(node)
        if node.get('type') == 'trigger' and isinstance(node.get('trigger'), dict):
            node['trigger'].setdefault('binding_id', _new_binding_id('t'))
    return copied


def normalize_rule_shape(rule: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(rule, dict):
        raise ValueError('规则必须是对象')
    obsolete = set(rule) & {'condition', 'event', 'trigger', 'actions', 'preconditions', '_matcher'}
    if obsolete:
        raise ValueError('旧条件模型已移除，不支持字段: ' + '、'.join(sorted(obsolete)))
    if not isinstance(rule.get('workflow'), dict):
        raise ValueError('规则必须提供 workflow 图')
    pending = [(rule, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError('规则嵌套不能超过 64 层')
        if isinstance(item, dict):
            pending.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)
    return copy.deepcopy(rule)


def normalize_rules(rules: Any) -> List[Dict[str, Any]]:
    if not isinstance(rules, list):
        raise ValueError('rules 必须是列表')
    identities: set[str] = set()
    return [ensure_rule_binding_ids(ensure_rule_id(normalize_rule_shape(rule), identities)) for rule in rules]
