"""插件自有数据的包装和检查。"""

import copy
import json
from typing import Any, Dict, Tuple


OWNED_VALUE_KEY = "$type"
MAX_OWNED_VALUE_BYTES = 1024 * 1024
MAX_SUMMARY_LENGTH = 160


class OwnedValueError(ValueError):
    """插件自有数据缺少归属信息或内容无效。"""


def qualified_data_type(package_name: str, data_type: str, version: int) -> str:
    return f"{package_name}/{data_type}@{version}"


def make_owned_value(
    package_name: str,
    data_type: str,
    version: int,
    data: Any,
    summary: str,
) -> Dict[str, Any]:
    if not isinstance(summary, str) or not summary.strip():
        raise OwnedValueError("插件提交数据时必须提供摘要")
    summary = summary.strip()
    if len(summary) > MAX_SUMMARY_LENGTH:
        raise OwnedValueError(f"插件数据摘要最多 {MAX_SUMMARY_LENGTH} 个字符")
    value = {
        OWNED_VALUE_KEY: qualified_data_type(package_name, data_type, version),
        "summary": summary,
        "data": copy.deepcopy(data),
    }
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise OwnedValueError("插件数据必须能够保存为 JSON") from exc
    if len(encoded) > MAX_OWNED_VALUE_BYTES:
        raise OwnedValueError(
            f"插件数据不能超过 {MAX_OWNED_VALUE_BYTES // 1024} KiB"
        )
    return value


def unpack_owned_value(
    value: Any,
    package_name: str,
    data_type: str,
    version: int,
) -> Any:
    if not isinstance(value, dict):
        raise OwnedValueError("插件数据必须是对象")
    expected = qualified_data_type(package_name, data_type, version)
    actual = value.get(OWNED_VALUE_KEY)
    if actual != expected:
        raise OwnedValueError(f"插件数据归属不匹配，应为 {expected}")
    if "data" not in value:
        raise OwnedValueError("插件数据缺少 data 字段")
    return copy.deepcopy(value["data"])


def owned_value_identity(value: Any) -> Tuple[str, str, int] | None:
    if not isinstance(value, dict):
        return None
    identity = value.get(OWNED_VALUE_KEY)
    if not isinstance(identity, str) or "/" not in identity or "@" not in identity:
        return None
    owner, typed_version = identity.rsplit("/", 1)
    data_type, raw_version = typed_version.rsplit("@", 1)
    try:
        version = int(raw_version)
    except ValueError:
        return None
    if not owner or not data_type or version < 1:
        return None
    return owner, data_type, version


def owned_value_summary(value: Any) -> str:
    if owned_value_identity(value) is None:
        return ""
    summary = value.get("summary")
    return summary if isinstance(summary, str) else ""


def value_matches_type(value: Any, value_type: str) -> bool:
    if value_type == "any":
        return True
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "bool":
        return isinstance(value, bool)
    if value_type == "array":
        return isinstance(value, list)
    if value_type == "object":
        return isinstance(value, dict)
    return False
