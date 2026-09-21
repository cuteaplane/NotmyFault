"""帮插件代码定位插件目录里自带的二进制和资源文件，路径解析带防逃逸校验。"""

import os
from functools import partial
from types import ModuleType


def plugin_resource(plugin_id: str, *relative_parts: str) -> str:
    raise ValueError("资源路径需要通过插件加载器或插件上下文解析")


def resolve_plugin_resource(roots: dict[str, str], plugin_id: str, *relative_parts: str) -> str:
    root = roots.get(plugin_id)
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


def resource_module(roots: dict[str, str]) -> ModuleType:
    module = ModuleType(__name__)
    module.plugin_resource = partial(resolve_plugin_resource, roots)
    return module
