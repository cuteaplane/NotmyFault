from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from notmyfault.core.conditions import (
    _absence_nodes, _condition_children, _condition_op, _event_key, _is_event_leaf,
    _positive_events, config_fingerprint, iter_condition_events,
)


DEFAULT_TTL_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class Retention:
    seconds: float
    inclusive: bool = False


@dataclass(frozen=True, slots=True)
class MatcherSpec:
    node: Any
    leaves: Mapping[str, tuple[dict, ...]]
    fingerprints: Mapping[int, str]
    keys: Mapping[int, str]
    positive_keys: frozenset[str]
    absences: tuple[dict, ...]
    retention: Mapping[str, Retention]
    subtree_keys: Mapping[int, frozenset[str]]


def build_matcher(node, default_ttl=DEFAULT_TTL_SECONDS):
    leaves = defaultdict(list)
    keys, fingerprints = {}, {}
    for leaf in iter_condition_events(node):
        leaves[leaf["type"]].append(leaf)
        keys[id(leaf)] = _event_key(leaf)
        fingerprints[id(leaf)] = config_fingerprint(leaf.get("params"))
    retention, subtree_keys = {}, {}

    def visit(current, limits):
        if not isinstance(current, dict):
            return frozenset()
        if _is_event_leaf(current):
            owned = frozenset({keys[id(current)]})
        elif _condition_op(current) == "not":
            owned = frozenset({"not:" + keys[id(_condition_children(current)[0])]})
        else:
            if _condition_op(current) == "all":
                window = current.get("within_seconds")
                limits = (*limits, Retention(float(window), True) if window is not None else
                          Retention(float(current.get("ttl_seconds", default_ttl))))
                if window is not None and "ttl_seconds" in current:
                    limits = (*limits, Retention(float(current["ttl_seconds"])))
            owned = frozenset(key for child in _condition_children(current) for key in visit(child, limits))
        if _is_event_leaf(current) or _condition_op(current) == "not":
            limit = min(limits or (Retention(default_ttl),), key=lambda item: (item.seconds, item.inclusive))
            retention.update({key: limit for key in owned})
        subtree_keys[id(current)] = owned
        return owned

    visit(node, ())
    return MatcherSpec(node, MappingProxyType({key: tuple(value) for key, value in leaves.items()}),
                       MappingProxyType(fingerprints), MappingProxyType(keys),
                       frozenset(_event_key(leaf) for leaf in _positive_events(node)), tuple(_absence_nodes(node)),
                       MappingProxyType(retention), MappingProxyType(subtree_keys))
