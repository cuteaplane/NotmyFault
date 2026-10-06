from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from notmyfault.core.bindings import iter_references, validate_binding_path, validate_reference_fields
from notmyfault.core.conditions import (
    _absence_nodes, _event_key, _positive_events, config_fingerprint, get_rule_condition, get_rule_events,
)
from notmyfault.core.data_types import DataTypeError, type_at_path
from notmyfault.core.rules import iter_action_nodes
from notmyfault.core.value_codec import encode_value


def _immutable(*args, **kwargs):
    raise TypeError("编译后的规则不能修改")


class FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


class FrozenList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, memo):
        return self


def freeze(value):
    if isinstance(value, (FrozenDict, FrozenList)):
        return value
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(freeze(item) for item in value)
    return copy.deepcopy(value)


def thaw(value):
    if isinstance(value, dict):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, list):
        return [thaw(item) for item in value]
    return copy.deepcopy(value)


def digest(value) -> str:
    encoded = json.dumps(encode_value(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SubscriptionSpec:
    trigger_id: str
    fingerprint: str
    leaf_id: str


@dataclass(frozen=True, slots=True)
class ReferenceAccessor:
    scope: str
    node: str | None
    path: tuple[str | int, ...]
    static_type: Mapping[str, Any]
    event_types: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class MatcherSpec:
    node: FrozenDict | None
    leaves: Mapping[str, tuple[FrozenDict, ...]]
    fingerprints: Mapping[int, str]
    keys: Mapping[int, str]
    positive_keys: frozenset[str]
    absences: tuple[FrozenDict, ...]


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    index: int
    location: str
    action: FrozenDict
    needs_sensitive_tracking: bool


@dataclass(frozen=True, slots=True)
class CompiledRule:
    rule_id: str
    digest: str
    source: FrozenDict
    subscriptions: tuple[SubscriptionSpec, ...]
    matcher_spec: MatcherSpec
    plan: tuple[Step, ...]
    step_index: Mapping[str, int]
    top_level_index: Mapping[str, int]
    concurrency: FrozenDict
    references: Mapping[int, ReferenceAccessor]


def _reference_type(schema, path, registry):
    try:
        return freeze(type_at_path(schema or {"type": "any"}, path, registry)[0])
    except DataTypeError as error:
        if error.code != "unknown_output":
            raise
        return freeze({"type": "any"})


def compile_rule(rule, template, triggers_meta, actions_meta, event_types, registry, plugin_digests=None) -> CompiledRule:
    source = freeze(rule)
    leaves = get_rule_events(source)
    by_event = defaultdict(list)
    subscriptions = []
    fingerprints, keys = {}, {}
    for leaf in leaves:
        by_event[leaf["type"]].append(leaf)
        fingerprint = config_fingerprint(leaf.get("params", {}))
        fingerprints[id(leaf)] = fingerprint
        keys[id(leaf)] = _event_key(leaf)
        subscriptions.append(SubscriptionSpec(leaf["type"], fingerprint, leaf["binding_id"]))
    matcher = MatcherSpec(get_rule_condition(source), MappingProxyType({key: tuple(items) for key, items in by_event.items()}),
                          MappingProxyType(fingerprints), MappingProxyType(keys),
                          frozenset(_event_key(leaf) for leaf in _positive_events(get_rule_condition(source))),
                          tuple(_absence_nodes(get_rule_condition(source))))
    references = {}
    for usage in iter_references(source):
        ref = usage.reference
        validate_reference_fields(ref, usage.location)
        scope, node = ref["scope"], ref.get("node")
        path = validate_binding_path(ref.get("path", []), usage.location, ref)
        sources = template.get("_binding_types", {}).get(scope, {})
        root = sources if scope == "event" else sources.get(node, {"type": "any"})
        static_type = _reference_type(root, path, registry)
        variants = {name: _reference_type(schema, path, registry) for name, schema in event_types.items() if name in by_event} if scope == "event" else {}
        references[id(ref)] = ReferenceAccessor(scope, node, path, freeze(static_type), MappingProxyType(variants))
    plan = tuple(Step(action["binding_id"], index, location, action,
                      bool(template.get("_sensitive_sources")) or any(field.get("sensitive") for field in actions_meta.get(action["type"], {}).get("outputs", []) if isinstance(field, dict)))
                 for index, (action, location) in enumerate(iter_action_nodes(source.get("actions", []))))
    plugin_ids = {("trigger", leaf["type"]) for leaf in leaves} | {("action", step.action["type"]) for step in plan if step.action["type"] not in {"if", "set_variable"}}
    plugins = {kind + ":" + name: ((plugin_digests or {}).get(kind + ":" + name) or digest((triggers_meta if kind == "trigger" else actions_meta).get(name, {}))) for kind, name in plugin_ids}
    semantic_rule = {key: value for key, value in source.items() if key != "name"}
    concurrency = {"mode": "single", **source.get("concurrency", {})}
    return CompiledRule(source["rule_id"], digest({"rule": semantic_rule, "plugins": plugins}), source, tuple(subscriptions), matcher,
                        plan, MappingProxyType({step.id: step.index for step in plan}),
                        MappingProxyType({action["binding_id"]: index for index, action in enumerate(source.get("actions", []))}),
                        freeze(concurrency), MappingProxyType(references))
