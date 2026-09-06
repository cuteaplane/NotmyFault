"""引用来源的类型结构和规则表达式检查。"""

from __future__ import annotations

from notmyfault.core.bindings import (
    BindingResolutionError, _validate_path, contains_dynamic_value,
    contains_legacy_template, is_literal, is_reference, is_typed_value, iter_references,
)
from notmyfault.core.data_types import (
    DataTypeError, field_type, infer_type, is_custom_type, normalize_type,
    normalize_value, type_at_path, types_compatible,
)
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.variables import initialize_variables, variable_definitions
from notmyfault.security.plugin_schema import literal_only_params


def output_type(meta, *, parameters=False):
    properties, required = {}, []
    for item in meta.get("params" if parameters else "outputs", []):
        field = {"name": item, "type": "any"} if isinstance(item, str) else item
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            continue
        properties[field["name"]] = field_type(field, parameter=parameters)
        if field.get("required") is not False:
            required.append(field["name"])
    return normalize_type({"type": "object", "properties": properties, "required": required, "additional_properties": False})


def rule_binding_types(rule, triggers_meta, actions_meta):
    from notmyfault.core.rules import get_rule_events, iter_action_nodes

    definitions = variable_definitions(rule)
    result = {"trigger": {}, "trigger_config": {}, "step": {}}
    for scope, items in definitions.items():
        result[scope] = {identity: definition["value_type"] for identity, definition in items.items()}
    for leaf in get_rule_events(rule):
        identity = leaf.get("binding_id")
        if not identity:
            continue
        meta = triggers_meta.get(leaf.get("type"), {})
        result["trigger"][identity] = output_type(meta)
        result["trigger_config"][identity] = output_type(meta, parameters=True)
    for action, _location in iter_action_nodes(rule.get("actions", [])):
        identity = action.get("binding_id")
        if not identity:
            continue
        if action.get("type") == "set_variable":
            schema = result["variable"].get(action.get("variable"), {"type": "any"})
            result["step"][identity] = output_type({"outputs": [{"name": "value", "type": schema}]})
        else:
            result["step"][identity] = output_type(actions_meta.get(action.get("type"), {}))
    return result


def prepare_binding_context(rule, context, triggers_meta, actions_meta):
    from notmyfault.core.rules import get_rule_events, iter_action_nodes

    if context.get("_variables_initialized"):
        return
    registry = TypeRegistry.from_plugins(triggers_meta, actions_meta)
    initialize_variables(rule, context, registry, overrides=context.get("manual_test", {}).get("variable_values"))
    sources = rule_binding_types(rule, triggers_meta, actions_meta)
    sources["event"] = output_type(triggers_meta.get(context.get("event", {}).get("type"), {}))
    context["_binding_types"].update(sources)

    def mark(scope, identity, fields):
        paths = [[field["name"]] for field in fields or [] if isinstance(field, dict) and field.get("sensitive")]
        if paths:
            context["_sensitive_sources"].setdefault(scope, {})[identity] = paths

    mark("event", "", triggers_meta.get(context.get("event", {}).get("type"), {}).get("outputs"))
    for leaf in get_rule_events(rule):
        meta = triggers_meta.get(leaf.get("type"), {})
        mark("trigger", leaf.get("binding_id"), meta.get("outputs"))
        mark("trigger_config", leaf.get("binding_id"), meta.get("params"))
    for action, _location in iter_action_nodes(rule.get("actions", [])):
        mark("step", action.get("binding_id"), actions_meta.get(action.get("type"), {}).get("outputs"))


def check_rule_bindings(rule, triggers_meta, actions_meta):
    from notmyfault.core.rules import (
        _absence_nodes, _positive_events, get_rule_condition, get_rule_events,
        iter_action_nodes,
    )

    issues = []

    def add(code, location, message, reference=None):
        issues.append({"code": code, "location": location, "reference": reference, "message": message})

    try:
        registry = TypeRegistry.from_plugins(triggers_meta, actions_meta)
        sources = rule_binding_types(rule, triggers_meta, actions_meta)
        initialize_variables(rule, {}, registry)
    except (DataTypeError, BindingResolutionError) as error:
        add(error.code, error.location, str(error))
        return issues
    leaves = list(_positive_events(get_rule_condition(rule)))
    leaves_by_id = {leaf.get("binding_id"): leaf for leaf in leaves}
    all_actions = {action.get("binding_id") for action, _path in iter_action_nodes(rule.get("actions", []))}

    def reference_type(reference, location, available):
        scope = reference.get("scope")
        node = reference.get("node")
        path = reference.get("path")
        try:
            _validate_path(path, location, reference)
        except BindingResolutionError as error:
            add(error.code, location, str(error), reference)
            return None
        policy = reference.get("on_missing")
        if set(reference) - {"scope", "node", "path", "on_missing", "default"} or policy not in (None, "error", "skip", "default") or ("default" in reference) != (policy == "default"):
            add("invalid_reference", location, "引用字段或缺失处理方式无效", reference)
            return None
        if scope in ("trigger", "trigger_config"):
            if node not in leaves_by_id:
                add("unknown_source", location, "引用的触发条件不存在", reference)
                return None
            root = sources[scope][node]
        elif scope in ("constant", "variable"):
            if node not in sources[scope]:
                add("unknown_source", location, "引用的常量或变量不存在", reference)
                return None
            root = sources[scope][node]
        elif scope == "step":
            if node not in all_actions:
                add("unknown_source", location, "引用的动作步骤不存在", reference)
                return None
            if node not in available:
                add("forward_reference", location, "只能引用当前路径中已经完成的动作", reference)
                return None
            root = sources[scope][node]
        elif scope == "event":
            if list(_absence_nodes(get_rule_condition(rule))):
                add("conditional_source", location, "NOT 超时事件不提供触发器数据，请引用具体触发节点", reference)
                return None
            candidates = [output_type(triggers_meta.get(leaf.get("type"), {})) for leaf in leaves]
            if not candidates:
                add("unknown_output", location, "没有提供该字段的触发事件", reference)
                return None
            root = candidates[0] if len(candidates) == 1 else normalize_type({"type": "union", "variants": candidates})
        else:
            add("invalid_reference", location, "未知的数据源 scope", reference)
            return None
        try:
            result, optional = type_at_path(root, path, registry)
            registry.require_shared(result)
        except DataTypeError as error:
            add(error.code, location, str(error), reference)
            return None
        if optional and policy is None:
            add("optional_output", location, "该字段可能不存在，请选择报错、跳过或默认值", reference)
            return None
        if policy == "default":
            check(reference["default"], result, f"{location}.$ref.default", available, strict=True)
        return result

    def check(value, target, location, available, *, strict=False):
        target = normalize_type(target)
        actual = None
        if is_reference(value):
            actual = reference_type(value["$ref"], location, available)
        elif is_literal(value):
            validate_literal(value["$literal"], target, location)
            return infer_type(value["$literal"])
        elif is_typed_value(value):
            if strict:
                validate_literal(value, target, location)
            return infer_type(value)
        elif isinstance(value, dict) and set(value) == {"$convert"}:
            conversion = value["$convert"]
            if not isinstance(conversion, dict) or not {"value", "to"} <= set(conversion) or set(conversion) - {"value", "to", "from", "options"}:
                add("invalid_conversion", location, "$convert 需要 value 和 to")
                return None
            try:
                actual = normalize_type(conversion["to"])
                if "from" in conversion:
                    normalize_type(conversion["from"])
            except DataTypeError as error:
                add(error.code, location, str(error))
                return None
            check(conversion["value"], "any", f"{location}.$convert.value", available)
        elif isinstance(value, dict) and set(value) == {"$template"}:
            if not isinstance(value["$template"], list):
                add("invalid_template", location, "$template 必须是数组")
                return None
            for index, part in enumerate(value["$template"]):
                check(part, "text", f"{location}.$template[{index}]", available, strict=True)
            actual = {"type": "text"}
        elif isinstance(value, dict):
            for name, child in value.items():
                child_type = "any"
                if target["type"] == "object":
                    child_type = target["properties"].get(name, target["additional_properties"])
                    if child_type is False:
                        if strict:
                            add("unknown_output", f"{location}.{name}", "对象未声明该字段")
                        child_type = "any"
                    elif child_type is True:
                        child_type = "any"
                check(child, child_type, f"{location}.{name}", available, strict=strict)
            if strict and not contains_dynamic_value(value):
                validate_literal(value, target, location)
            return infer_type(value)
        elif isinstance(value, list):
            child_type = target["items"] if target["type"] == "array" else "any"
            for index, child in enumerate(value):
                check(child, child_type, f"{location}[{index}]", available, strict=strict)
            if strict and target["type"] not in ("array", "any", "union"):
                validate_literal(value, target, location)
            return infer_type(value)
        elif strict and not contains_legacy_template(value):
            validate_literal(value, target, location)
            return infer_type(value)
        if actual is not None and not types_compatible(actual, target, registry):
            add("binding_type_mismatch", location, f"{actual['type']} 数据不能绑定到 {target['type']} 参数", value.get("$ref") if is_reference(value) else None)
        return actual

    def validate_literal(value, target, location):
        try:
            normalize_value(value, target, registry, location=location)
        except DataTypeError as error:
            add(error.code, location, str(error))

    def params(item, meta, location, available):
        definitions = {field.get("name"): field for field in meta.get("params", []) if isinstance(field, dict)}
        values = item.get("params", {})
        if not isinstance(values, dict):
            return
        for name, value in values.items():
            field = definitions.get(name, {})
            target = field_type(field, parameter=True) if field else {"type": "any"}
            path = f"{location}.params.{name}"
            if name in literal_only_params(meta) and contains_dynamic_value(value):
                add("unsafe_dynamic_parameter", path, f"参数 {name!r} 只允许使用固定值")
            elif field.get("type") == "plugin_data" and contains_dynamic_value(value):
                add("private_plugin_data", path, "插件自有数据不能绑定运行数据")
            else:
                check(value, target, path, available, strict="value_type" in field and field.get("type") != "plugin_data")

    def sequence(items, location, previous):
        available = set(previous)
        for index, action in enumerate(items):
            if not isinstance(action, dict):
                continue
            path = f"{location}[{index}]"
            if action.get("type") == "if":
                check(action.get("condition"), "any", f"{path}.condition", available)
                for branch in ("then", "else"):
                    sequence(action.get(branch, []), f"{path}.{branch}", available)
            elif action.get("type") == "set_variable":
                identity = action.get("variable")
                if identity not in sources["variable"]:
                    add("readonly_constant" if identity in sources["constant"] else "unknown_source", path, "赋值目标必须是已声明的运行变量")
                else:
                    check(action.get("value"), sources["variable"][identity], f"{path}.value", available, strict=True)
                available.add(action.get("binding_id"))
            else:
                params(action, actions_meta.get(action.get("type"), {}), path, available)
                sequence(action.get("failure_actions", []), f"{path}.failure_actions", available)
                available.add(action.get("binding_id"))

    for index, event in enumerate(get_rule_events(rule)):
        params(event, triggers_meta.get(event.get("type"), {}), f"condition[{index}]", set())
        for usage in iter_references(event.get("params", {})):
            if usage.reference.get("scope") != "constant":
                add("invalid_reference", f"condition[{index}].params", "触发器配置只能引用规则常量", usage.reference)
    sequence(rule.get("actions", []), "actions", set())
    return issues
