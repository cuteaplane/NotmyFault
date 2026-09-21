"""NotmyFault 包入口在 strict 模式限制外部导入，并导出 run 和 __version__"""

import os as _os
import sys as _sys

__all__ = ["run", "__version__"]

_PROJECT_ROOT = _os.path.realpath(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
)


def _in_pytest() -> bool:
    import inspect

    pytest_module = _sys.modules.get("_pytest.python")
    if pytest_module is None:
        return False
    entry_codes = {
        function.__code__
        for name in ("importtestmodule", "pytest_runtest_call")
        if inspect.isfunction(function := getattr(pytest_module, name, None))
    }
    for frame_info in inspect.stack():
        if frame_info.frame.f_code in entry_codes:
            return True
    return False


def _find_external_caller():
    """查找首个不在 notmyfault 和 importlib 内部的调用帧"""
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
    """判断调用者文件是否位于项目根目录"""
    file_path = caller.f_globals.get("__file__") or ""
    if not file_path:
        return False
    try:
        real = _os.path.realpath(file_path)
        return _os.path.commonpath([real, _PROJECT_ROOT]) == _PROJECT_ROOT
    except (ValueError, OSError):
        return False


def _guard_package_import() -> None:
    """按安全模式限制源码包的外部导入"""
    if getattr(_sys, "frozen", False):
        return
    caller = _find_external_caller()
    arguments = getattr(_sys, "orig_argv", ())
    module_entry = any(
        arguments[index:index + 2] == ["-m", "notmyfault"]
        for index in range(len(arguments) - 1)
    )
    if caller is None or module_entry or _in_pytest() or _is_project_script(caller):
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
        f"NMF_STRICT_IMPORT_DENIED: NotmyFault 安全限制（strict）：拒绝 pytest 之外的外部导入"
        f"（调用者 {caller_name} @ {caller_file}）。notmyfault 组件仅供引擎自身、"
        f"官方入口脚本与插件使用，请通过 NOTMYFAULT.pyw 启动。"
    )


_guard_package_import()

from .host.app import run
from .version import __version__
