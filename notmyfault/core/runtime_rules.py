"""准备触发器配置和每次运行的常量、变量与引用类型"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType

from notmyfault.core.compiled_rules import CompiledRule, compile_rule, digest, freeze
from notmyfault.core.rule_model import _RULE_ID_RE

from notmyfault.core.binding_schema import output_type, prepare_binding_context, protect_context_inputs
from notmyfault.core.data_types import normalize_fields
from notmyfault.core.rule_model import normalize_rules
from notmyfault.core.rules import (
    ConditionRuntime, aggregate_trigger_params, get_rule_events,
    normalize_rule_input, validate_rules,
)
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.variables import resolve_trigger_constants
from notmyfault.values import hold_value


@dataclass
class PreparedRules:
    rules: list[dict]
    registry: TypeRegistry
    contexts: dict[str, dict]
    trigger_params: dict[str, list[dict]]
    event_types: dict[str, dict]
    compiled: dict[str, CompiledRule] = field(default_factory=dict)
    event_index: dict[str, tuple[CompiledRule, ...]] = field(default_factory=dict)
    document_digest: str = ""
    generation: object | None = None
    plugins: object | None = None
    total: int = 0
    issues: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    runtime: ConditionRuntime = field(default_factory=ConditionRuntime)
    trigger_metadata: dict = field(default_factory=dict)
    input_digests: dict[str, str] = field(default_factory=dict)
    rule_warnings: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    validated: bool = False

    def __len__(self):
        return len(self.rules)

    def prepare_context(self, rule, context):
        template = self.contexts[rule_key(rule)]
        context.update(template)
        context["variables"] = dict(template["variables"])
        context["_binding_types"] = dict(template["_binding_types"])
        compiled = self.compiled[rule_key(rule)]
        context["_compiled_refs"] = compiled.references
        context["_compiled_rule"] = compiled
        context["_generation"] = self.generation
        event_type = context.get("event", {}).get("type", "")
        context["_binding_types"]["event"] = self.event_types.get(event_type, output_type({}))
        protect_context_inputs(context, self.plugins.triggers_meta if self.plugins is not None else self.trigger_metadata, self.registry)


def rule_key(rule):
    return rule["rule_id"]


def _fill_owner_rule_id(leaf, meta, rule):
    """触发器声明了 owner_rule_id 就补上所属规则

    这个参数用来挡掉"规则监视自己"的循环，手工写或接口写入的规则常常不带它。
    """
    declared = meta.get("params")
    if not isinstance(declared, list) or not any(
        isinstance(spec, dict) and spec.get("name") == "owner_rule_id"
        for spec in declared
    ):
        return
    params = leaf.get("params")
    if not isinstance(params, dict):
        return
    if str(params.get("owner_rule_id", "") or "").strip():
        return
    params["owner_rule_id"] = rule_key(rule)


def validate_rule_identifiers(rules):
    if not isinstance(rules, list):
        raise ValueError("rules 必须是列表")
    identifiers = [rule.get("rule_id") if isinstance(rule, dict) else None for rule in rules]
    if any(not isinstance(identity, str) or not _RULE_ID_RE.fullmatch(identity) for identity in identifiers):
        raise ValueError("编译规则必须提供有效的 rule_id")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("rule_id 与其他规则重复")


def prepare_rules(rules, triggers_meta, actions_meta, *, validate=True, resolve_trigger=None, plugin_digests=None, clock=None,
                  previous=None):
    validate_rule_identifiers(rules)
    normalized = normalize_rule_input(rules) if validate else normalize_rules(rules)
    reuse = (previous is not None and previous.plugins is not None
             and previous.plugins.digests == plugin_digests and (previous.validated or not validate))
    registry = previous.registry if reuse else TypeRegistry.from_plugins(triggers_meta, actions_meta)
    prepared, contexts = [], {}
    input_digests, reused, cached_warnings = {}, {}, {}
    issues, warnings = [], []
    trigger_ready = {}
    for rule in normalized:
        identity = rule_key(rule)
        input_digests[identity] = digest(rule)
        if (reuse and identity in previous.compiled
                and previous.input_digests.get(identity) == input_digests[identity]
                and (resolve_trigger is None or all(resolve_trigger(leaf["type"]) is not None
                                                   for leaf in get_rule_events(rule)))):
            compiled = previous.compiled[identity]
            contexts[identity] = previous.contexts[identity]
            prepared.append(compiled.source)
            reused[identity] = compiled
            cached_warnings[identity] = previous.rule_warnings.get(identity, [])
            if validate:
                warnings.extend(cached_warnings[identity])
            continue
        if validate:
            valid, _, rule_issues, rule_warnings = validate_rules([rule], triggers_meta, actions_meta)
            issues.extend(rule_issues)
            warnings.extend(rule_warnings)
            cached_warnings[identity] = rule_warnings
            if not valid:
                continue
        try:
            protected_configs = {}
            resolved = resolve_trigger_constants(rule, registry, protected_configs=protected_configs)
            for leaf in get_rule_events(resolved):
                meta = triggers_meta.get(leaf.get("type"), {})
                leaf["params"] = normalize_fields(
                    leaf.get("params", {}), meta.get("params"), registry, parameters=True,
                )
                _fill_owner_rule_id(leaf, meta, resolved)
                protected_leaf = {**leaf, "params": normalize_fields(
                    protected_configs[leaf["binding_id"]], meta.get("params"), registry, parameters=True,
                )}
                _fill_owner_rule_id(protected_leaf, meta, resolved)
                protected_configs[leaf["binding_id"]] = hold_value(protected_leaf["params"])
            context = {"_trigger_configs": hold_value(protected_configs)}
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
        contexts[rule_key(resolved)] = {key: value if key == "_type_registry" else hold_value(value, blobs=False)
                                      for key, value in context.items()}
        prepared.append(resolved)
    result = PreparedRules(
        rules=prepared,
        registry=registry,
        contexts=contexts,
        trigger_params=aggregate_trigger_params(prepared),
        event_types={name: output_type(meta) for name, meta in triggers_meta.items()},
        total=len(normalized),
        document_digest=digest(normalized),
        issues=issues,
        warnings=warnings,
        runtime=ConditionRuntime(clock=clock),
        trigger_metadata=triggers_meta,
        input_digests=input_digests,
        rule_warnings=cached_warnings,
        validated=validate,
    )
    compiled = {rule_key(rule): reused[rule_key(rule)] if rule_key(rule) in reused else
                compile_rule(rule, contexts[rule_key(rule)], triggers_meta, actions_meta, result.event_types, registry, plugin_digests)
                for rule in prepared}
    index = {}
    for rule in compiled.values():
        for event_type in rule.matcher_spec.leaves:
            index.setdefault(event_type, []).append(rule)
    result.compiled = MappingProxyType(compiled)
    result.event_index = MappingProxyType({name: tuple(items) for name, items in index.items()})
    result.rules = freeze([rule.source for rule in compiled.values()])
    result.runtime.configure(compiled)
    return result
