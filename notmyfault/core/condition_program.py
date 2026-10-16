from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from notmyfault.core.canvas_workflow import CanvasProgram, validate_canvas_graph
from notmyfault.core.conditions import config_fingerprint


DEFAULT_TTL_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class Retention:
    seconds: float
    inclusive: bool = False


@dataclass(frozen=True, slots=True)
class AbsenceSpec:
    node_id: str
    key: str
    seconds: float
    repeat: str
    reset_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatcherSpec:
    graph: CanvasProgram
    roots: tuple[str, ...]
    leaves: Mapping[str, tuple[str, ...]]
    fingerprints: Mapping[str, str]
    keys: Mapping[str, str]
    positive_keys: frozenset[str]
    absences: tuple[AbsenceSpec, ...]
    retention: Mapping[str, Retention]
    dependency_keys: Mapping[str, frozenset[str]]


def build_matcher(rule, default_ttl=DEFAULT_TTL_SECONDS, *, roots=None):
    if not isinstance(rule, dict) or not isinstance(rule.get('workflow'), dict):
        raise ValueError('事件匹配必须提供原生 workflow 图')
    errors = validate_canvas_graph(rule['workflow'])
    if errors:
        raise ValueError('；'.join(errors))
    graph = CanvasProgram(rule)
    roots = graph.roots() if roots is None else tuple(roots)
    leaves, fingerprints, keys = defaultdict(list), {}, {}
    retention, dependencies, absences = {}, {}, {}
    visiting, positive = set(), set()

    def limit_for(name, limits):
        value = min(limits or (Retention(default_ttl),), key=lambda item: (item.seconds, item.inclusive))
        existing = retention.get(name)
        if existing is None or (value.seconds, value.inclusive) < (existing.seconds, existing.inclusive):
            retention[name] = value

    def visit(identity, limits=()):
        if identity in visiting:
            raise ValueError('事件匹配依赖不能包含环')
        visiting.add(identity)
        node = graph.nodes[identity]
        kind, op = node['type'], node.get('op', node.get('group_type'))
        if kind == 'condition':
            owned = frozenset()
        elif kind == 'trigger':
            leaf = node['trigger']
            name = 'id:' + leaf['binding_id']
            keys[identity] = name
            if identity not in leaves[leaf['type']]:
                leaves[leaf['type']].append(identity)
            fingerprints[identity] = config_fingerprint(leaf.get('params', {}))
            if node.get('occurrence') == 'absent':
                absence_key = 'absence:' + identity
                absences[identity] = AbsenceSpec(identity, absence_key, float(node.get('duration_seconds', 10)),
                    node.get('repeat', 'once_until_seen'), (identity,))
                limit_for(absence_key, limits)
                owned = frozenset({absence_key})
            else:
                positive.add(name)
                limit_for(name, limits)
                owned = frozenset({name})
        else:
            if op == 'all':
                window = node.get('within_seconds')
                limits = (*limits, Retention(float(window), True) if window is not None
                    else Retention(float(node.get('ttl_seconds', default_ttl))))
            children = graph.dependencies(identity)
            if kind == 'action_group':
                owned = frozenset(key for child in children for key in visit(child, limits))
            elif op == 'not':
                if len(children) != 1:
                    raise ValueError('NOT 匹配必须有一个输入')
                child_keys = visit(children[0], limits)
                if 'duration_seconds' in node:
                    name = 'absence:' + identity
                    absences[identity] = AbsenceSpec(identity, name, float(node['duration_seconds']),
                        node.get('repeat', 'once_until_seen'), children)
                    limit_for(name, limits)
                    owned = frozenset({name})
                else:
                    owned = child_keys
            else:
                owned = frozenset(key for child in children for key in visit(child, limits))
        dependencies[identity] = dependencies.get(identity, frozenset()) | owned
        visiting.remove(identity)
        return owned

    for root in roots:
        visit(root)
    return MatcherSpec(graph, roots, MappingProxyType({key: tuple(value) for key, value in leaves.items()}),
        MappingProxyType(fingerprints), MappingProxyType(keys), frozenset(positive), tuple(absences.values()),
        MappingProxyType(retention), MappingProxyType(dependencies))
