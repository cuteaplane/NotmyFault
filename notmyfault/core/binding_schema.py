"""引用来源的类型结构和规则表达式检查。"""

from __future__ import annotations

from notmyfault.core.bindings import (
    BindingResolutionError, validate_binding_path, validate_reference_fields, contains_dynamic_value,
    is_literal, is_reference, is_typed_value, iter_references,
)
from notmyfault.core.data_types import (
    DataTypeError, field_type, infer_type, normalize_type,
    normalize_value, normalize_fields, type_at_path, types_compatible,
)
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.values import hold_value
from notmyfault.core.variables import initialize_variables, variable_definitions
from notmyfault.security.plugin_schema import literal_only_params
from notmyfault.core.workflow_graph import (
    iter_rule_actions, get_all_rule_events, graph_available_steps, graph_index, graph_possible_steps, group_exit_steps,
)


def output_type(meta, *, parameters=False):
    key = "params" if parameters else "outputs"
    properties, required = {}, []
    for item in meta.get(key, []):
        if not isinstance(item, dict):
            raise DataTypeError(f"{key} 字段声明必须是对象", location=key, code="invalid_field")
        if not isinstance(item.get("name"), str):
            continue
        properties[item["name"]] = field_type(item, parameter=parameters)
        if item.get("required") is not False:
            required.append(item["name"])
    return normalize_type({"type": "object", "properties": properties, "required": required, "additional_properties": False})


def expression_type(value, sources, registry):
    if is_literal(value):
        return infer_type(value["$literal"])
    if is_reference(value):
        reference = value["$ref"]
        root = sources.get(reference.get("scope"), {}).get(reference.get("node"), {"type": "any"})
        return type_at_path(root, reference.get("path", []), registry)[0]
    if is_typed_value(value):
        return infer_type(value)
    if isinstance(value, dict):
        if set(value) == {"$convert"}:
            return normalize_type(value["$convert"]["to"])
        if set(value) == {"$template"}:
            return {"type": "text"}
        return normalize_type({"type": "object", "properties": {name: expression_type(item, sources, registry) for name, item in value.items()},
            "required": list(value), "additional_properties": False})
    if isinstance(value, list):
        variants = []
        for item in value:
            schema = expression_type(item, sources, registry)
            if schema not in variants:
                variants.append(schema)
        items = variants[0] if len(variants) == 1 else {"type": "union", "variants": variants} if variants else {"type": "any"}
        return normalize_type({"type": "array", "items": items})
    return infer_type(value)


def rule_binding_types(rule, triggers_meta, actions_meta):
    definitions = variable_definitions(rule)
    result = {"trigger": {}, "trigger_config": {}, "step": {}}
    for scope, items in definitions.items():
        result[scope] = {identity: definition["value_type"] for identity, definition in items.items()}
    for leaf in get_all_rule_events(rule):
        identity = leaf.get("binding_id")
        if not identity:
            continue
        meta = triggers_meta.get(leaf.get("type"), {})
        result["trigger"][identity] = output_type(meta)
        result["trigger_config"][identity] = output_type(meta, parameters=True)
    for action, _location in iter_rule_actions(rule):
        identity = action.get("binding_id")
        if not identity:
            continue
        if action.get("type") == "set_variable":
            schema = result["variable"].get(action.get("variable"), {"type": "any"})
            result["step"][identity] = output_type({"outputs": [{"name": "value", "type": schema}]})
        elif action.get("type") in ("action_group", "event_group"):
            result["step"][identity] = {"type": "object", "properties": {}, "required": [], "additional_properties": True}
        else:
            result["step"][identity] = output_type(actions_meta.get(action.get("type"), {}))
    registry = TypeRegistry.from_plugins(triggers_meta, actions_meta)
    groups = {action["binding_id"]: action for action, _location in iter_rule_actions(rule) if action.get("type") == "action_group"}
    pending, finished = set(), set()
    def exports(identity):
        if identity in finished:
            return
        if identity in pending:
            raise DataTypeError("动作组导出存在循环引用", location=f"{identity}.exports", code="cyclic_reference")
        pending.add(identity)
        action = groups[identity]
        for usage in iter_references(action.get("exports", {})):
            reference = usage.reference
            if reference.get("scope") == "step" and reference.get("node") in groups:
                exports(reference["node"])
        fields = {}
        for name, expression in action.get("exports", {}).items():
            try:
                fields[name] = expression_type(expression, result, registry)
            except (DataTypeError, KeyError, TypeError):
                fields[name] = {"type": "any"}
        result["step"][identity] = normalize_type({"type": "object", "properties": fields, "required": list(fields), "additional_properties": False})
        pending.remove(identity)
        finished.add(identity)
    for identity in groups:
        exports(identity)
    return result


def prepare_binding_context(rule, context, triggers_meta, actions_meta, *, registry=None):
    if context.get("_variables_initialized"):
        return
    if registry is None:
        registry = TypeRegistry.from_plugins(triggers_meta, actions_meta)
    initialize_variables(rule, context, registry, overrides=context.get("manual_test", {}).get("variable_values"))
    sources = rule_binding_types(rule, triggers_meta, actions_meta)
    sources["event"] = output_type(triggers_meta.get(context.get("event", {}).get("type"), {}))
    context["_binding_types"].update(sources)

    protect_context_inputs(context, triggers_meta, registry)


def rule_step_output_fields(rule, triggers_meta, actions_meta):
    sources = rule_binding_types(rule, triggers_meta, actions_meta)
    definitions = variable_definitions(rule)
    actions = {node['binding_id']: node for node, _path in iter_rule_actions(rule)}
    triggers = {node['binding_id']: node for node in get_all_rule_events(rule)}

    def sensitive_expression(value, visiting):
        return any(sensitive_reference(usage.reference, visiting) for usage in iter_references(value))

    def sensitive_reference(reference, visiting):
        scope, identity = reference.get('scope'), reference.get('node')
        key = (scope, identity)
        if key in visiting:
            return False
        visiting = visiting | {key}
        if scope in definitions:
            declaration = definitions[scope].get(identity, {})
            return declaration.get('sensitive', False) or sensitive_expression(declaration.get('value' if scope == 'constant' else 'initial'), visiting)
        path = reference.get('path', [])
        field = path[0] if path else None
        if scope == 'step':
            node = actions.get(identity, {})
            if node.get('type') == 'set_variable':
                return sensitive_reference({'scope': 'variable', 'node': node.get('variable')}, visiting) or sensitive_expression(node.get('value'), visiting)
            if node.get('type') == 'action_group':
                exports = node.get('exports', {})
                values = [exports.get(field)] if field is not None else exports.values()
                return any(sensitive_expression(value, visiting) for value in values)
            meta, fields_key = actions_meta.get(node.get('type'), {}), 'outputs'
        elif scope in ('trigger', 'trigger_config'):
            meta = triggers_meta.get(triggers.get(identity, {}).get('type'), {})
            fields_key = 'params' if scope == 'trigger_config' else 'outputs'
        else:
            return False
        return any(isinstance(item, dict) and item.get('sensitive') and (field is None or item.get('name') == field) for item in meta.get(fields_key, []))

    return {identity: [{'name': name, 'type': schema, 'sensitive': sensitive_reference(
                {'scope': 'step', 'node': identity, 'path': [name]}, set())}
            for name, schema in declaration.get('properties', {}).items()]
            for identity, declaration in sources['step'].items()}


def protect_context_inputs(context, triggers_meta, registry=None):
    event = context.get("event", {})
    manual = context.get("manual_test")
    fields = triggers_meta.get(event.get("type"), {}).get("outputs")
    if manual is not None:
        names = {field["name"] for trigger in context.get("triggers", {}).values()
                 for field in triggers_meta.get(trigger.get("type"), {}).get("outputs", [])
                 if isinstance(field, dict) and field.get("sensitive") and "name" in field}
        fields = [{"name": name, "type": "any", "sensitive": True} for name in names]
    event["payload"] = hold_value(normalize_fields(event.get("payload", {}), fields, registry, wrap_sensitive=True, partial=manual is not None))
    for identity, trigger in context.get("triggers", {}).items():
        meta = triggers_meta.get(trigger.get("type"), {})
        fields = meta.get("outputs")
        if manual is not None and identity not in manual.get("provided_trigger_ids", ()):
            fields = []
        trigger["payload"] = hold_value(normalize_fields(trigger.get("payload", {}), fields, registry, wrap_sensitive=True))
        config = context.get("_trigger_configs", {}).get(identity, trigger.get("config", {}))
        trigger["config"] = hold_value(normalize_fields(config, meta.get("params"), registry, parameters=True, wrap_sensitive=True))


def check_rule_bindings(rule, triggers_meta, actions_meta):
    from notmyfault.core.condition_program import build_matcher


    from notmyfault.core.canvas_workflow import CanvasProgram
    active = CanvasProgram(rule).active_nodes()
    issues = []
    conditional_steps = set()

    def add(code, location, message, reference=None):
        issues.append({"code": code, "location": location, "reference": reference, "message": message})

    try:
        registry = TypeRegistry.from_plugins(triggers_meta, actions_meta)
        sources = rule_binding_types(rule, triggers_meta, actions_meta)
        initialize_variables(rule, {}, registry)
    except (DataTypeError, BindingResolutionError) as error:
        add(error.code, error.location, str(error))
        return issues
    leaves = get_all_rule_events(rule)
    leaves_by_id = {leaf.get("binding_id"): leaf for leaf in leaves}
    all_actions = {action.get("binding_id") for action, _path in iter_rule_actions(rule)}

    def reference_type(reference, location, available, *, allow_missing=False):
        scope = reference.get("scope")
        node = reference.get("node")
        path = reference.get("path")
        try:
            validate_binding_path(path, location, reference)
            validate_reference_fields(reference, location)
        except BindingResolutionError as error:
            add(error.code, location, str(error), reference)
            return None
        policy = reference.get("on_missing")
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
                if node not in conditional_steps:
                    add("forward_reference", location, "只能引用当前路径中已经完成的动作", reference)
                    return None
                if policy is None and not allow_missing:
                    add("conditional_source", location, "来源动作可能未参与本次运行，请设置缺失值处理", reference)
                    return None
            root = sources[scope][node]
        elif scope == "event":
            if build_matcher(rule).absences:
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
        if optional and policy is None and not allow_missing:
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
        elif strict:
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
                if "value_type" not in field and field.get("options") and not contains_dynamic_value(value):
                    target = {**target, "enum": [option["value"] if isinstance(option, dict) else option for option in field["options"]]}
                check(value, target, path, available, strict=bool(field) and field.get("type") != "plugin_data")

    def predicate(node, location, available):
        if not isinstance(node, dict):
            return
        op = node.get("op")
        if op in ("exists", "not_exists") and is_reference(node.get("left")):
            reference_type(node["left"]["$ref"], f"{location}.left", available, allow_missing=True)
        elif op != "run_status":
            target = "any"
            if op in ("is_true", "is_false"):
                target = "bool"
            elif op in ("gt", "gte", "lt", "lte"):
                target = "number"
            if "left" in node:
                check(node["left"], target, f"{location}.left", available, strict=True)
            if "right" in node:
                check(node["right"], target, f"{location}.right", available, strict=True)

    for node, location in iter_rule_actions(rule):
        if node.get('type') != 'trigger' or node['binding_id'] not in active:
            continue
        event = node['trigger']
        params(event, triggers_meta.get(event['type'], {}), f'{location}.trigger', set())
        for usage in iter_references(event.get('params', {})):
            if usage.reference.get('scope') != 'constant':
                add('invalid_reference', f'{location}.trigger.params', '触发器配置只能引用规则常量', usage.reference)
    def graph_bindings(graph, location, inherited, inherited_possible=None):
        nonlocal conditional_steps
        try:
            available_by_id = graph_available_steps(graph, inherited)
            possible_by_id = graph_possible_steps(graph, inherited if inherited_possible is None else inherited_possible)
        except (KeyError, RecursionError):
            add("invalid_workflow", location, "工作流控制关系无效")
            return
        nodes = graph_index(graph)
        for index, action in enumerate(graph.get("nodes", [])):
            if action["binding_id"] not in active:
                continue
            path = f"{location}.nodes[{index}]"
            available = available_by_id.get(action.get("binding_id"), set(inherited))
            conditional_steps = possible_by_id.get(action.get("binding_id"), set(inherited)) - available
            kind = action.get("type")
            if kind == 'condition':
                predicate(action.get('predicate'), f'{path}.predicate', available)
            elif kind == 'action_group':
                exported = group_exit_steps(graph, action['binding_id'], inherited)
                possible = group_exit_steps(graph, action['binding_id'], inherited, possible=True)
                conditional_steps = possible - exported
                for name, value in action.get('exports', {}).items():
                    check(value, 'any', f'{path}.exports.{name}', exported)
            elif kind == "set_variable":
                target = sources["variable"].get(action.get("variable"))
                if target is None:
                    add("unknown_source", path, "赋值目标必须是已声明的运行变量")
                else:
                    check(action.get("value"), target, f"{path}.value", available, strict=True)
            elif kind not in ("merge", "end", "trigger", "logic", "event_group"):
                params(action, actions_meta.get(kind, {}), path, available)
    graph_bindings(rule["workflow"], "workflow", set())
    return issues
