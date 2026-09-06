"""规则常量和单次运行内的变量。"""

from __future__ import annotations

import copy
import re

from notmyfault.core.bindings import contains_legacy_template, is_expression, is_typed_value, iter_references, resolve_value
from notmyfault.core.data_types import DataTypeError, normalize_type, normalize_value


_IDENTITY = re.compile(r"^[cv]_[a-z0-9_]{6,64}$")


def variable_definitions(rule):
    result = {"constant": {}, "variable": {}}
    for field, scope, prefix in (("constants", "constant", "c_"), ("variables", "variable", "v_")):
        definitions = rule.get(field, [])
        if not isinstance(definitions, list):
            raise DataTypeError("定义必须是数组", location=field, code="invalid_variable")
        for index, definition in enumerate(definitions):
            location = f"{field}[{index}]"
            if not isinstance(definition, dict):
                raise DataTypeError("变量定义必须是对象", location=location, code="invalid_variable")
            identity = definition.get("id")
            if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity) or not identity.startswith(prefix):
                raise DataTypeError(f"id 必须以 {prefix} 开头，后接 6 到 64 位小写字母、数字或下划线", location=location, code="invalid_variable")
            if identity in result[scope]:
                raise DataTypeError("变量 id 重复", location=location, code="duplicate_variable")
            if not isinstance(definition.get("name"), str) or not definition["name"].strip():
                raise DataTypeError("名称不能为空", location=location, code="invalid_variable")
            allowed = {"id", "name", "value_type", "sensitive", "value" if scope == "constant" else "initial"}
            if set(definition) - allowed:
                raise DataTypeError("变量定义包含未知字段", location=location, code="invalid_variable")
            if "sensitive" in definition and not isinstance(definition["sensitive"], bool):
                raise DataTypeError("sensitive 必须为布尔值", location=location, code="invalid_variable")
            if scope == "constant" and "value" not in definition:
                raise DataTypeError("常量必须设置 value", location=location, code="missing_value")
            result[scope][identity] = {
                **copy.deepcopy(definition),
                "value_type": normalize_type(definition.get("value_type"), location=f"{location}.value_type"),
            }
    return result


def expression_is_sensitive(value, context):
    sources = context.get("_sensitive_sources", {})
    for usage in iter_references(value):
        reference = usage.reference
        paths = sources.get(reference.get("scope"), {}).get(reference.get("node", ""), [])
        selected = reference.get("path", [])
        for marked in paths:
            overlap = min(len(selected), len(marked))
            if selected[:overlap] == marked[:overlap]:
                return True
    return False


def _constant_dependencies(expression, location):
    if contains_legacy_template(expression):
        raise DataTypeError("初始化只支持结构化常量引用，原样文本请使用 $literal", location=location, code="invalid_variable")
    references = list(iter_references(expression, location=location))
    for usage in references:
        if usage.reference.get("scope") != "constant":
            raise DataTypeError("初始化只能引用规则常量", location=usage.location, code="invalid_variable")
    return references


def constant_context(rule, registry=None):
    definitions = variable_definitions(rule)
    if registry is not None:
        for entries in definitions.values():
            for definition in entries.values():
                registry.require_shared(definition["value_type"])
    context = {
        "constants": {}, "_type_registry": registry,
        "_binding_types": {scope: {key: item["value_type"] for key, item in entries.items()} for scope, entries in definitions.items()},
        "_sensitive_sources": {"constant": {}, "variable": {}},
    }
    pending = set()

    def evaluate(identity):
        if identity in context["constants"]:
            return
        if identity in pending:
            raise DataTypeError("常量存在循环引用", location=f"constants.{identity}", code="cyclic_constant")
        definition = definitions["constant"].get(identity)
        if definition is None:
            raise DataTypeError("引用的常量不存在", location=f"constants.{identity}", code="unknown_source")
        pending.add(identity)
        location = f"constants.{identity}.value"
        for usage in _constant_dependencies(definition["value"], location):
            evaluate(usage.reference.get("node"))
        value = resolve_value(definition["value"], context, location=location)
        context["constants"][identity] = normalize_value(value, definition["value_type"], registry, location=location)
        if registry is not None:
            context["constants"][identity] = registry.binding_value(context["constants"][identity])
        if definition.get("sensitive") or expression_is_sensitive(definition["value"], context):
            context["_sensitive_sources"]["constant"][identity] = [[]]
        pending.remove(identity)

    for identity in definitions["constant"]:
        evaluate(identity)
    return context, definitions


def initialize_variables(rule, context, registry=None, *, overrides=None):
    prepared, definitions = constant_context(rule, registry)
    values = {}
    if overrides is not None and not isinstance(overrides, dict):
        raise DataTypeError("测试变量必须是对象", code="invalid_variable")
    if overrides and set(overrides) - set(definitions["variable"]):
        raise DataTypeError("测试输入引用了未声明的变量", code="unknown_source")
    for identity, definition in definitions["variable"].items():
        location = f"variables.{identity}.initial"
        if "initial" in definition:
            _constant_dependencies(definition["initial"], location)
            value = resolve_value(definition["initial"], prepared, location=location)
            values[identity] = normalize_value(value, definition["value_type"], registry, location=location)
        if overrides is not None and identity in overrides:
            values[identity] = normalize_value(overrides[identity], definition["value_type"], registry, location=f"test.variables.{identity}")
        if registry is not None and identity in values:
            values[identity] = registry.binding_value(values[identity])
        if definition.get("sensitive") or expression_is_sensitive(definition.get("initial"), prepared):
            prepared["_sensitive_sources"]["variable"][identity] = [[]]
    context["constants"] = prepared["constants"]
    context["variables"] = values
    context.setdefault("_binding_types", {}).update(prepared["_binding_types"])
    context.setdefault("_sensitive_sources", {}).update(prepared["_sensitive_sources"])
    context["_variable_definitions"] = definitions["variable"]
    context["_type_registry"] = registry
    context["_variables_initialized"] = True


def assign_variable(identity, expression, context):
    definition = context.get("_variable_definitions", {}).get(identity)
    if definition is None:
        if identity in context.get("constants", {}):
            raise DataTypeError("常量只读，不能赋值", code="readonly_constant")
        raise DataTypeError("赋值目标不是已声明的运行变量", code="unknown_source")
    location = f"variables.{identity}"
    value = resolve_value(expression, context, location=location)
    value = normalize_value(value, definition["value_type"], context.get("_type_registry"), location=location)
    if context.get("_type_registry") is not None:
        value = context["_type_registry"].binding_value(value)
    sensitive = definition.get("sensitive") or expression_is_sensitive(expression, context)
    context["variables"][identity] = value
    marked = context.setdefault("_sensitive_sources", {}).setdefault("variable", {})
    if sensitive:
        marked[identity] = [[]]
    else:
        marked.pop(identity, None)
    return copy.deepcopy(value)


def resolve_trigger_constants(rule, registry=None):
    from notmyfault.core.rules import get_rule_events

    context, _definitions = constant_context(rule, registry)
    resolved = copy.deepcopy(rule)

    def resolve_config(value, location):
        if is_typed_value(value):
            return copy.deepcopy(value)
        if is_expression(value):
            _constant_dependencies(value, location)
            return resolve_value(value, context, location=location)
        if isinstance(value, dict):
            return {key: resolve_config(child, f"{location}.{key}") for key, child in value.items()}
        if isinstance(value, list):
            return [resolve_config(child, f"{location}[{index}]") for index, child in enumerate(value)]
        return copy.deepcopy(value)

    for index, event in enumerate(get_rule_events(resolved)):
        params = event.get("params", {})
        event["params"] = resolve_config(params, f"condition[{index}].params")
    return resolved
