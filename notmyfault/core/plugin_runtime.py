from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from notmyfault.core.compiled_rules import digest, freeze
from notmyfault.core.workflow_graph import get_all_rule_events, iter_rule_actions, NATIVE_NODES


@dataclass(frozen=True, slots=True)
class PluginSnapshot:
    triggers_meta: Mapping[str, dict]
    triggers_funcs: Mapping[str, Any]
    actions_meta: Mapping[str, dict]
    actions_funcs: Mapping[str, Any]
    modules: Mapping[str, Any]
    digests: Mapping[str, str]


class PluginRuntime:
    def __init__(self, registry, loader) -> None:
        self.registry = registry
        self.loader = loader

    def snapshot(self, rules) -> PluginSnapshot:
        triggers = {leaf["type"] for rule in rules for leaf in get_all_rule_events(rule)}
        actions = {action["type"] for rule in rules for action, _ in iter_rule_actions(rule)}
        for plugin_id in sorted(triggers):
            if plugin_id not in self.registry.triggers_funcs:
                self.registry.resolve_trigger(plugin_id)
        for plugin_id in sorted(actions - NATIVE_NODES):
            if plugin_id not in self.registry.actions_funcs:
                self.registry.resolve_action(plugin_id)
        triggers_meta = freeze(self.registry.triggers_meta)
        actions_meta = freeze(self.registry.actions_meta)
        versions = {f"{kind}:{identity}": digest({"meta": meta, "source": self.registry.source_digests.get(identity, "")})
                    for kind, metadata in (("trigger", triggers_meta), ("action", actions_meta)) for identity, meta in metadata.items()}
        return PluginSnapshot(triggers_meta, MappingProxyType(dict(self.registry.triggers_funcs)),
                              actions_meta, MappingProxyType(dict(self.registry.actions_funcs)),
                              MappingProxyType(dict(self.registry.modules)), MappingProxyType(versions))
