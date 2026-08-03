"""规则运行数据绑定的解析、遍历与错误报告"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Tuple


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


def is_reference(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"$ref"}
        and isinstance(value["$ref"], dict)
    )


def _validate_path(path: Any, location: str, reference: Any) -> Tuple[str, ...]:
    if not isinstance(path, list):
        raise BindingResolutionError(
            "invalid_reference", location, reference, "$ref.path 必须是字符串数组"
        )
    result = []
    for segment in path:
        if (
            not isinstance(segment, str)
            or not _SEGMENT_RE.fullmatch(segment)
            or segment.startswith("__")
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
    path = _validate_path(reference.get("path", []), location, reference)
    if scope == "event":
        return context.get("event", {}).get("payload", _MISSING), path

    node = reference.get("node")
    if not isinstance(node, str) or not node:
        raise BindingResolutionError(
            "invalid_reference", location, reference, "$ref.node 不能为空"
        )
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
        return step.get("result", _MISSING), path
    raise BindingResolutionError(
        "invalid_reference",
        location,
        reference,
        f"未知的数据源 scope: {scope!r}",
    )


def resolve_reference(
    reference: Dict[str, Any],
    context: Dict[str, Any],
    *,
    location: str,
) -> Any:
    current, path = _reference_root(reference, context, location)
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            current = _MISSING
            break
        current = current[segment]
    if current is _MISSING:
        raise BindingResolutionError(
            "missing_binding_value",
            location,
            reference,
            f"运行数据不存在: {reference}",
        )
    return current


def _lookup_legacy(context: Dict[str, Any], dotted_path: str) -> Any:
    current: Any = context
    for segment in dotted_path.split("."):
        if (
            not _SEGMENT_RE.fullmatch(segment)
            or segment.startswith("__")
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
        return resolved

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
    if is_reference(value):
        yield BindingUsage(location, value["$ref"])
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from iter_references(item, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_references(item, location=f"{location}[{index}]")


def references_available(value: Any, context: Dict[str, Any]) -> bool:
    """返回结构化绑定的来源是否参与了本次工作流运行"""
    for usage in iter_references(value):
        reference = usage.reference
        scope = reference.get("scope")
        if scope in ("trigger", "trigger_config"):
            if reference.get("node") not in context.get("triggers", {}):
                return False
        elif scope == "step":
            step = context.get("steps", {}).get(reference.get("node"))
            if not isinstance(step, dict) or step.get("status") != "ok":
                return False
    return True


def iter_legacy_event_payload_paths(value: Any) -> Iterable[Tuple[str, ...]]:
    """遍历旧模板中需要手动测试输入的 ``event.payload`` 路径"""
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
