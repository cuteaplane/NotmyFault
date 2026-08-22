"""插件能依赖的宿主 API 版本

manifest 写 "engines": {"notmyfault_api": 1} 声明依赖。引擎只认和自己一样的
版本号：号不同就跳过并给出诊断，不做 semver 范围解析——宿主 API 还年轻，
用整数最直白，等真出现 v2 再考虑范围语法。
"""

HOST_API_VERSION = 1


def engines_compatibility(meta: dict) -> tuple[bool, str]:
    """返回清单是否匹配当前宿主 API 版本，不匹配时带原因"""
    engines = meta.get("engines") if isinstance(meta, dict) else None
    if not isinstance(engines, dict) or not engines:
        return True, ""
    required = engines.get("notmyfault_api")
    if required is None:
        return True, ""
    if required == HOST_API_VERSION:
        return True, ""
    return (
        False,
        f"插件要求宿主 API 版本 {required}，当前是 {HOST_API_VERSION}",
    )
