"""准备触发器配置和每次运行独立复制的初始值"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from notmyfault.core.binding_schema import output_type, prepare_binding_context
from notmyfault.core.data_types import normalize_fields
from notmyfault.core.rule_model import normalize_rules
from notmyfault.core.rules import (
    ConditionRuntime, aggregate_trigger_params, get_rule_events,
    normalize_rule_input, validate_rules,
)
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.variables import resolve_trigger_constants


@dataclass
class PreparedRules:
    rules: list[dict]
    registry: TypeRegistry
    contexts: dict[str, dict]
    trigger_params: dict[str, list[dict]]
    event_types: dict[str, dict]
    event_sensitive: dict[str, list[list[str]]]
    total: int = 0
    issues: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    runtime: ConditionRuntime = field(default_factory=ConditionRuntime)

    def __len__(self):
        return len(self.rules)

    def prepare_context(self, rule, context):
        template = self.contexts[rule_key(rule)]
        context.update(copy.deepcopy(template, {id(self.registry): self.registry}))
        event_type = context.get("event", {}).get("type", "")
        context["_binding_types"]["event"] = self.event_types.get(event_type, output_type({}))
        paths = self.event_sensitive.get(event_type)
        if paths:
            context["_sensitive_sources"]["event"] = {"": paths}


def rule_key(rule):
    return str(rule.get("rule_id") or rule.get("name", ""))


def prepare_rules(rules, triggers_meta, actions_meta, *, validate=True, resolve_trigger=None):
    normalized = normalize_rule_input(rules) if validate else normalize_rules(rules)
    registry = TypeRegistry.from_plugins(triggers_meta, actions_meta)
    prepared, contexts = [], {}
    issues, warnings = [], []
    trigger_ready = {}
    for rule in normalized:
        if validate:
            valid, _, rule_issues, rule_warnings = validate_rules([rule], triggers_meta, actions_meta)
            issues.extend(rule_issues)
            warnings.extend(rule_warnings)
            if not valid:
                continue
        try:
            resolved = resolve_trigger_constants(rule, registry)
            for leaf in get_rule_events(resolved):
                meta = triggers_meta.get(leaf.get("type"), {})
                leaf["params"] = normalize_fields(
                    leaf.get("params", {}), meta.get("params"), registry, parameters=True,
                )
            context = {}
            prepare_binding_context(resolved, context, triggers_meta, actions_meta, registry=registry)
            if resolve_trigger is not None:
                for leaf in get_rule_events(resolved):
                    trigger_id = leaf["type"]
                    if trigger_id not in trigger_ready:
                        trigger_ready[trigger_id] = resolve_trigger(trigger_id) is not None
                    if not trigger_ready[trigger_id]:
                        raise ValueError(f"触发器未能加载: {trigger_id}")
        except ValueError as error:
            if not validate:
                raise
            issues.append((rule["name"], str(error)))
            continue
        contexts[rule_key(resolved)] = context
        prepared.append(resolved)
    return PreparedRules(
        rules=prepared,
        registry=registry,
        contexts=contexts,
        trigger_params=aggregate_trigger_params(prepared),
        event_types={name: output_type(meta) for name, meta in triggers_meta.items()},
        event_sensitive={
            name: [[spec["name"]] for spec in meta.get("outputs", [])
                   if isinstance(spec, dict) and spec.get("sensitive")]
            for name, meta in triggers_meta.items()
        },
        total=len(normalized),
        issues=issues,
        warnings=warnings,
    )
