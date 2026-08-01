"""NotmyFault 包入口：导入守卫 + 公共 API。

导入安全模型：
- notmyfault 组件之间可以互相导入（引擎内部协作）。
- 官方入口脚本（项目根下的 NOTMYFAULT.pyw / dashboard.pyw / build.py /
  pack_plugin.py / tests 等）与 pytest 允许导入。
- strict 模式：除上述之外的一切导入行为都会被拒绝，防止外部代码把
  notmyfault 组件当库随意加载；normal / permissive 模式打印日志后放行。

notmyfault.security.sudo 有更严的专用守卫（见 sudo.py）：只允许插件命名空间
（notmyfault.action_* / trigger_*）与引擎核心（engine.py）导入。
"""

import os as _os
import sys as _sys

__all__ = ["run", "__version__"]

_PROJECT_ROOT = _os.path.realpath(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
)


def _in_pytest() -> bool:
    return "pytest" in _sys.modules or "_pytest" in _sys.modules


def _find_external_caller():
    """定位第一个不属于 notmyfault 包、也不是 importlib 机制的调用帧。"""
    import inspect
    for frame_info in inspect.stack():
        name = frame_info.frame.f_globals.get("__name__", "")
        if (
            name.startswith("notmyfault")
            or name.startswith("importlib")
            or name.startswith("_frozen_importlib")
        ):
            continue
        return frame_info.frame
    return None


def _is_project_script(caller) -> bool:
    """调用者文件位于项目根目录内（官方入口脚本）即视为可信。"""
    file_path = caller.f_globals.get("__file__") or ""
    if not file_path:
        return False
    try:
        real = _os.path.realpath(file_path)
        return _os.path.commonpath([real, _PROJECT_ROOT]) == _PROJECT_ROOT
    except (ValueError, OSError):
        return False


def _guard_package_import() -> None:
    """strict 模式拒绝 pytest 之外的外部导入；宽松模式打印日志放行。

    冻结构建（PyInstaller）由打包者控制运行环境，源码级守卫无意义，
    与 engine 的核心完整性校验（仅非 frozen 生效）保持一致。
    """
    if getattr(_sys, "frozen", False):
        return
    caller = _find_external_caller()
    if caller is None or _in_pytest() or _is_project_script(caller):
        return
    from notmyfault.security.security import detect_security_mode
    mode = detect_security_mode()
    caller_name = caller.f_globals.get("__name__", "")
    caller_file = caller.f_globals.get("__file__") or "<stdin>"
    if mode is not None and mode.value != "strict":
        print(
            f"[Guard] 宽松模式（{mode.value}）：外部代码导入 notmyfault"
            f"（调用者 {caller_name} @ {caller_file}），放行",
            file=_sys.stderr,
        )
        return
    raise ImportError(
        f"NotmyFault 安全限制（strict）：拒绝 pytest 之外的外部导入"
        f"（调用者 {caller_name} @ {caller_file}）。notmyfault 组件仅供引擎自身、"
        f"官方入口脚本与插件使用，请通过 NOTMYFAULT.pyw 启动。"
    )


_guard_package_import()

from .host.app import run
from .version import __version__
