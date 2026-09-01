"""帮插件代码定位插件目录里自带的二进制和资源文件，路径解析带防逃逸校验。"""

import os
from typing import Any, Optional

_registry: Optional[Any] = None


def set_registry(registry: Any) -> None:
    """引擎构造插件注册表后注入，plugin_resource 靠它查到插件根目录。"""
    global _registry
    _registry = registry


def plugin_resource(plugin_id: str, *relative_parts: str) -> str:
    """返回插件目录内资源的绝对路径，插件未装载或路径越界时抛 ValueError。"""
    if _registry is None:
        raise ValueError("插件注册表尚未初始化")
    root = _registry.plugin_roots.get(plugin_id)
    if root is None:
        raise ValueError(f"插件未装载或不存在: {plugin_id}")
    if not relative_parts:
        raise ValueError("资源路径不能为空")
    relative = os.path.join(*relative_parts)
    resolved = os.path.realpath(os.path.join(root, relative))
    real_root = os.path.realpath(root)
    try:
        inside = os.path.commonpath((real_root, resolved)) == real_root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError(f"资源路径逃逸插件目录: {relative}")
    return resolved
