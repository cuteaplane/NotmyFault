from __future__ import annotations

import json
from typing import Any, Dict

from notmyfault.core.value_codec import encode_value
from notmyfault.core.predicates import values_equal


def check_event_params(event_def: Dict[str, Any], event_payload: Dict[str, Any]) -> bool:
    """检查事件参数是否匹配并允许 payload 含额外字段"""
    expected_params = event_def.get("params", {})
    for key, expected_val in expected_params.items():
        # 缺少字段时不算命中
        if key not in event_payload or not values_equal(event_payload[key], expected_val):
            return False
    return True

def _canonical_config_value(value: Any) -> Any:
    """递归规范化配置值，等价写法使用相同指纹"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return int(value) if float(value).is_integer() else value
        except (OverflowError, ValueError):
            return value
    if isinstance(value, dict):
        return {str(key): _canonical_config_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_config_value(item) for item in value]
    return value

def config_fingerprint(params: Any) -> str:
    """生成 event-v2 触发器实例配置的稳定指纹"""
    if not isinstance(params, dict):
        params = {}
    return json.dumps(
        encode_value(_canonical_config_value(params)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
