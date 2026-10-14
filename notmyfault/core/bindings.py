"""规则运行数据绑定的解析、遍历与错误报告"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Tuple

from notmyfault.core.data_types import DataTypeError, infer_type, is_custom_type, normalize_type, type_at_path
from notmyfault.core.value_conversion import convert_value
from notmyfault.values import Blob, Secret, contains_secret, hold_value, plain_value, protect, secret_value


_MISSING = object()


@dataclass(frozen=True)
class BindingUsage:
    location: str
    reference: Dict[str, Any]


class BindingResolutionError(ValueError):
    """绑定解析失败说明配置写错，重试也没用"""

    def __init__(
        self,
        code: str,
        location: str,
        reference: Any,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.location = location
        self.reference = reference

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "location": self.location,
            "reference": self.reference,
            "message": str(self),
        }


class BindingSkip(BindingResolutionError):
    def __init__(self, location, reference):
        super().__init__("skip_missing_value", location, reference, "所需数据不存在，按配置跳过动作")


def is_literal(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"$literal"}


def is_typed_value(value):
    return isinstance(value, dict) and "data" in value and is_custom_type(value.get("$type"))


def is_expression(value: Any) -> bool:
    return isinstance(value, dict) and len(value) == 1 and next(iter(value)) in {"$ref", "$literal", "$convert", "$template"}


def is_reference(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"$ref"}
        and isinstance(value["$ref"], dict)
    )


def validate_binding_path(path: Any, location: str, reference: Any) -> tuple:
    if not isinstance(path, list):
        raise BindingResolutionError(
            "invalid_reference", location, reference, "$ref.path 必须是对象键和数组下标组成的数组"
        )
    result = []
    for segment in path:
        if (
            isinstance(segment, bool)
            or not isinstance(segment, (str, int))
            or isinstance(segment, int) and segment < 0
            or isinstance(segment, str) and (segment.startswith("_") or segment in {"constructor", "prototype"})
        ):
            raise BindingResolutionError(
                "invalid_reference",
                location,
                reference,
                f"非法的数据路径段: {segment!r}",
            )
        result.append(segment)
    return tuple(result)


def validate_reference_fields(reference: Dict[str, Any], location: str) -> None:
    unknown = set(reference) - {"scope", "node", "path", "on_missing", "default"}
    policy = reference.get("on_missing")
    if unknown or policy not in (None, "error", "skip", "default") or ("default" in reference) != (policy == "default"):
        raise BindingResolutionError("invalid_reference", location, reference, "引用字段或缺失处理方式无效")


def _reference_root(
    reference: Dict[str, Any],
    context: Dict[str, Any],
    location: str,
    validated_path: tuple | None = None,
) -> tuple[Any, Tuple[str, ...]]:
    scope = reference.get("scope")
    if validated_path is None:
        validate_reference_fields(reference, location)
        path = validate_binding_path(reference.get("path", []), location, reference)
    else:
        path = validated_path
    if scope == "event":
        return context.get("event", {}).get("payload", _MISSING), path

    node = reference.get("node")
    if not isinstance(node, str) or not node:
        raise BindingResolutionError(
            "invalid_reference", location, reference, "$ref.node 不能为空"
        )
    if scope in ("constant", "variable"):
        bucket = "constants" if scope == "constant" else "variables"
        return context.get(bucket, {}).get(node, _MISSING), path
    if scope == "trigger":
        trigger = context.get("triggers", {}).get(node, _MISSING)
        if trigger is _MISSING:
            return _MISSING, path
        return trigger.get("payload", _MISSING), path
    if scope == "trigger_config":
        trigger = context.get("triggers", {}).get(node, _MISSING)
        if trigger is _MISSING:
            return _MISSING, path
        return trigger.get("config", _MISSING), path
    if scope == "step":
        step = context.get("steps", {}).get(node, _MISSING)
        if step is _MISSING:
            return _MISSING, path
        if step.get("status") in ("failed", "cancelled", "timed_out"):
            raise BindingResolutionError("failed_binding_source", location, reference, "引用的动作执行失败，不能使用默认值代替")
        if step.get("status") == "skipped":
            return _MISSING, path
        return step.get("result", _MISSING), path
    raise BindingResolutionError(
        "invalid_reference",
        location,
        reference,
        f"未知的数据源 scope: {scope!r}",
    )


def _read_reference(
    reference: Dict[str, Any],
    context: Dict[str, Any],
    *,
    location: str,
) -> Any:
    accessor = context.get("_compiled_refs", {}).get(id(reference))
    current, path = _reference_root(reference, context, location, accessor.path if accessor is not None else None)
    sensitive = False
    for segment in path:
        if isinstance(current, Secret):
            sensitive = True
            current = secret_value(current)
        if isinstance(current, dict) and is_custom_type(current.get("$type")):
            registry = context.get("_type_registry")
            definition = registry.definition(current["$type"]) if registry is not None else None
            if definition is None or definition.get("binding") != "shared":
                raise BindingResolutionError("private_plugin_data", location, reference, "私有插件数据不支持字段引用")
        if isinstance(current, dict) and isinstance(segment, str) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and isinstance(segment, int) and segment < len(current):
            current = current[segment]
        else:
            return _MISSING
    return protect(current) if sensitive and current is not _MISSING else current


def reference_exists(reference, context, *, location):
    value = _read_reference(reference, context, location=location)
    if value is _MISSING:
        return False
    registry = context.get("_type_registry")
    if registry is not None:
        try:
            registry.binding_value(value)
        except DataTypeError as error:
            raise BindingResolutionError(error.code, location, reference, str(error)) from error
    return True


def resolve_reference(reference, context, *, location):
    current = _read_reference(reference, context, location=location)
    if current is _MISSING:
        if reference.get("on_missing") == "skip":
            raise BindingSkip(location, reference)
        if reference.get("on_missing") == "default":
            current = resolve_value(reference["default"], context, location=f"{location}.$ref.default")
        else:
            raise BindingResolutionError("missing_binding_value", location, reference, "引用的运行数据不存在")
    registry = context.get("_type_registry")
    if registry is not None:
        try:
            current = registry.binding_value(current)
        except DataTypeError as error:
            raise BindingResolutionError(error.code, location, reference, str(error)) from error
    return hold_value(current)


def expression_type(value, context):
    if is_reference(value):
        reference = value["$ref"]
        scope = reference.get("scope")
        accessor = context.get("_compiled_refs", {}).get(id(reference))
        if accessor is not None:
            if scope == "event":
                return accessor.event_types.get(context.get("event", {}).get("type"), accessor.static_type)
            return accessor.static_type
        sources = context.get("_binding_types", {}).get(scope, {})
        root = sources if scope == "event" else sources.get(reference.get("node"), {"type": "any"})
        if not root:
            root = {"type": "any"}
        return type_at_path(root, reference.get("path", []), context.get("_type_registry"))[0]
    if is_literal(value):
        return infer_type(value["$literal"])
    if isinstance(value, dict) and set(value) == {"$convert"}:
        return normalize_type(value["$convert"].get("to"))
    if isinstance(value, dict) and set(value) == {"$template"}:
        return {"type": "text"}
    return infer_type(value)


def resolve_value(
    value: Any,
    context: Dict[str, Any],
    *,
    location: str = "$",
) -> Any:
    if is_reference(value):
        return resolve_reference(value["$ref"], context, location=location)
    if is_literal(value):
        return copy.deepcopy(value["$literal"])
    if is_typed_value(value):
        return copy.deepcopy(value)
    if isinstance(value, dict) and set(value) == {"$convert"}:
        conversion = value["$convert"]
        if not isinstance(conversion, dict) or not {"value", "to"} <= set(conversion) or set(conversion) - {"value", "to", "from", "options"}:
            raise BindingResolutionError("invalid_conversion", location, None, "$convert 需要 value 和 to")
        try:
            source = conversion.get("from") or expression_type(conversion["value"], context)
            resolved = resolve_value(conversion["value"], context, location=f"{location}.$convert.value")
            converted = convert_value(
                plain_value(resolved),
                conversion["to"], context.get("_type_registry"), source=source,
                options=conversion.get("options"), location=location,
            )
            return protect(converted) if contains_secret(resolved) else converted
        except DataTypeError as error:
            raise BindingResolutionError(error.code, error.location, None, str(error)) from error
    if isinstance(value, dict) and set(value) == {"$template"}:
        parts = value["$template"]
        if not isinstance(parts, list):
            raise BindingResolutionError("invalid_template", location, None, "$template 必须是文本和数据引用组成的数组")
        text = []
        sensitive = False
        for index, part in enumerate(parts):
            resolved = resolve_value(part, context, location=f"{location}.$template[{index}]")
            sensitive = sensitive or isinstance(resolved, Secret)
            resolved = secret_value(resolved)
            if isinstance(resolved, Blob) and resolved.type == "text":
                resolved = resolved.text()
            if not isinstance(resolved, str):
                raise BindingResolutionError("binding_type_mismatch", location, None, "拼接项必须为文本，其他类型需要显式转换")
            text.append(resolved)
        return protect("".join(text)) if sensitive else "".join(text)
    if isinstance(value, dict):
        return {
            key: resolve_value(item, context, location=f"{location}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_value(item, context, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def iter_references(value: Any, *, location: str = "$") -> Iterable[BindingUsage]:
    if is_literal(value) or is_typed_value(value):
        return
    if is_reference(value):
        yield BindingUsage(location, value["$ref"])
        if "default" in value["$ref"]:
            yield from iter_references(value["$ref"]["default"], location=f"{location}.$ref.default")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from iter_references(item, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_references(item, location=f"{location}[{index}]")


def contains_dynamic_value(value):
    if is_literal(value) or is_typed_value(value):
        return False
    if is_expression(value):
        return True
    if isinstance(value, dict):
        return any(contains_dynamic_value(child) for child in value.values())
    if isinstance(value, list):
        return any(contains_dynamic_value(child) for child in value)
    return False


def unavailable_references(
    value: Any, context: Dict[str, Any]
) -> list[BindingUsage]:
    missing = []

    def visit(item, location):
        if is_literal(item) or is_typed_value(item):
            return
        if is_reference(item):
            reference = item["$ref"]
            scope = reference.get("scope")
            usage = BindingUsage(location, reference)
            policy = reference.get("on_missing")
            if policy is not None:
                try:
                    absent = _read_reference(reference, context, location=location) is _MISSING
                except BindingResolutionError:
                    return
                if absent and policy == "skip":
                    missing.append(usage)
                elif absent and policy == "default":
                    visit(reference["default"], f"{location}.$ref.default")
            elif scope in ("trigger", "trigger_config"):
                if reference.get("node") not in context.get("triggers", {}):
                    missing.append(usage)
            elif scope == "step":
                step = context.get("steps", {}).get(reference.get("node"))
                if not isinstance(step, dict) or step.get("status") != "ok":
                    missing.append(usage)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, f"{location}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{location}[{index}]")

    visit(value, "$")
    return missing


def references_available(value: Any, context: Dict[str, Any]) -> bool:
    return not unavailable_references(value, context)
