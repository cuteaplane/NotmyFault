"""规则运行数据绑定的解析、遍历与错误报告"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Tuple

from notmyfault.core.data_types import DataTypeError, infer_type, is_custom_type, normalize_type, type_at_path
from notmyfault.core.value_conversion import convert_value


_LEGACY_TEMPLATE = re.compile(r"{{\s*([a-zA-Z_][\w.]*)\s*}}")
_LEGACY_EVENT_PAYLOAD = re.compile(
    r"{{\s*event\.payload(?P<path>(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s*}}"
)
_SEGMENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
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


def _validate_path(path: Any, location: str, reference: Any) -> tuple:
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


def _reference_root(
    reference: Dict[str, Any],
    context: Dict[str, Any],
    location: str,
) -> tuple[Any, Tuple[str, ...]]:
    scope = reference.get("scope")
    unknown = set(reference) - {"scope", "node", "path", "on_missing", "default"}
    policy = reference.get("on_missing")
    if unknown or policy not in (None, "error", "skip", "default") or ("default" in reference) != (policy == "default"):
        raise BindingResolutionError("invalid_reference", location, reference, "引用字段或缺失处理方式无效")
    path = _validate_path(reference.get("path", []), location, reference)
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
    current, path = _reference_root(reference, context, location)
    for segment in path:
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
    return current


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
    return copy.deepcopy(current)


def expression_type(value, context):
    if is_reference(value):
        reference = value["$ref"]
        scope = reference.get("scope")
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


def _lookup_legacy(context: Dict[str, Any], dotted_path: str) -> Any:
    current: Any = context
    for segment in dotted_path.split("."):
        if (
            not _SEGMENT_RE.fullmatch(segment)
            or segment.startswith("_")
            or not isinstance(current, dict)
            or segment not in current
        ):
            return _MISSING
        current = current[segment]
    return current


def resolve_value(
    value: Any,
    context: Dict[str, Any],
    *,
    location: str = "$",
) -> Any:
    """递归解析 v2 ``$ref`` 与旧 ``{{ dotted.path }}`` 模板"""
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
            return convert_value(
                resolve_value(conversion["value"], context, location=f"{location}.$convert.value"),
                conversion["to"], context.get("_type_registry"), source=source,
                options=conversion.get("options"), location=location,
            )
        except DataTypeError as error:
            raise BindingResolutionError(error.code, error.location, None, str(error)) from error
    if isinstance(value, dict) and set(value) == {"$template"}:
        parts = value["$template"]
        if not isinstance(parts, list):
            raise BindingResolutionError("invalid_template", location, None, "$template 必须是文本和数据引用组成的数组")
        text = []
        for index, part in enumerate(parts):
            resolved = resolve_value(part, context, location=f"{location}.$template[{index}]")
            if not isinstance(resolved, str):
                raise BindingResolutionError("binding_type_mismatch", location, None, "拼接项必须为文本，其他类型需要显式转换")
            text.append(resolved)
        return "".join(text)
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
    if not isinstance(value, str):
        return value

    full = _LEGACY_TEMPLATE.fullmatch(value)
    if full:
        resolved = _lookup_legacy(context, full.group(1))
        if resolved is _MISSING:
            raise BindingResolutionError(
                "missing_binding_value",
                location,
                {"legacy": full.group(1)},
                f"运行数据不存在: {full.group(1)}",
            )
        return copy.deepcopy(resolved)

    def replace(match: re.Match[str]) -> str:
        resolved = _lookup_legacy(context, match.group(1))
        if resolved is _MISSING:
            raise BindingResolutionError(
                "missing_binding_value",
                location,
                {"legacy": match.group(1)},
                f"运行数据不存在: {match.group(1)}",
            )
        return str(resolved)

    return _LEGACY_TEMPLATE.sub(replace, value)


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


def contains_legacy_template(value: Any) -> bool:
    """递归判断字符串或嵌套结构里是否带旧 ``{{ }}`` 模板"""
    if is_literal(value) or is_typed_value(value):
        return False
    if isinstance(value, str):
        return bool(_LEGACY_TEMPLATE.search(value))
    if isinstance(value, dict):
        return any(contains_legacy_template(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_legacy_template(item) for item in value)
    return False


def contains_dynamic_value(value):
    if is_literal(value) or is_typed_value(value):
        return False
    if is_expression(value):
        return True
    if isinstance(value, dict):
        return any(contains_dynamic_value(child) for child in value.values())
    if isinstance(value, list):
        return any(contains_dynamic_value(child) for child in value)
    return contains_legacy_template(value)


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


def iter_legacy_event_payload_paths(value: Any) -> Iterable[Tuple[str, ...]]:
    """遍历旧模板中需要手动测试输入的 ``event.payload`` 路径"""
    if is_literal(value) or is_typed_value(value):
        return
    if isinstance(value, str):
        for match in _LEGACY_EVENT_PAYLOAD.finditer(value):
            suffix = match.group("path")
            yield tuple(segment for segment in suffix.split(".") if segment)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_legacy_event_payload_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_legacy_event_payload_paths(item)
