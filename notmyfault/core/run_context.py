from __future__ import annotations

from collections.abc import Mapping, MutableMapping

from notmyfault.values import FrozenDict, FrozenList, hold_value


class ReadOnlyMapping(Mapping):
    __slots__ = ("__source",)

    def __init__(self, source):
        self.__source = source

    def __getitem__(self, key):
        value = self.__source[key]
        if isinstance(value, (FrozenDict, FrozenList)):
            return value
        if isinstance(value, dict):
            return ReadOnlyMapping(value)
        if isinstance(value, list):
            return hold_value(value)
        return value

    def __iter__(self):
        return iter(self.__source)

    def __len__(self):
        return len(self.__source)


class RunContext(ReadOnlyMapping):
    pass


class RunState(MutableMapping):
    def __init__(self, values=None):
        self._prepare = None
        self.public = {key: value for key, value in (values or {}).items() if not key.startswith("_") and key != "manual_test"}
        self.private = {key: value for key, value in (values or {}).items() if key.startswith("_") or key == "manual_test"}

    def _bucket(self, key):
        return self.private if key.startswith("_") or key == "manual_test" else self.public

    def __getitem__(self, key):
        if key not in ("rule", "run") and (not key.startswith("_") or key in (
                "_variables_initialized", "_type_registry", "_binding_types", "_trigger_configs", "_variable_definitions")):
            self.prepare()
        return self._bucket(key)[key]

    def defer_preparation(self, prepare):
        self._prepare = prepare

    def prepare(self):
        if self._prepare is not None:
            prepare, self._prepare = self._prepare, None
            prepare(self)

    def __setitem__(self, key, value):
        self._bucket(key)[key] = value

    def __delitem__(self, key):
        del self._bucket(key)[key]

    def __iter__(self):
        self.prepare()
        return iter((*self.public, *self.private))

    def __len__(self):
        return len(self.public) + len(self.private)

    def view(self, cancellation):
        self.prepare()
        values = {key: self.public[key] for key in ("rule", "run", "event", "triggers", "steps", "constants", "variables") if key in self.public}
        values["runtime"] = {"cancellation": cancellation}
        return RunContext(values)


def run_view(context, cancellation):
    if isinstance(context, RunState):
        return context.view(cancellation)
    return RunState(context).view(cancellation)


def context_sources(expressions):
    from notmyfault.core.bindings import iter_references

    selected = []
    for usage in iter_references(expressions):
        ref = usage.reference
        scope = ref.get("scope")
        bucket = {"step": "steps", "trigger": "triggers", "trigger_config": "triggers",
                  "constant": "constants", "variable": "variables"}.get(scope)
        if bucket:
            selected.append((bucket, ref.get("node")))
        elif scope == "event":
            selected.append(("event", None))

    return tuple(dict.fromkeys(selected))


def referenced_context(context, expressions, *, sources=None):
    result = {key: context[key] for key in ("rule", "run") if key in context}
    for bucket, identity in context_sources(expressions) if sources is None else sources:
        if bucket == "event":
            result["event"] = context.get("event", {})
            continue
        if identity in context.get(bucket, {}):
            result.setdefault(bucket, {})[identity] = context[bucket][identity]
    result["_type_registry"] = context.get("_type_registry")
    return result
