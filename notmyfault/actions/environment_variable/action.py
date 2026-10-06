"""读写当前进程的环境变量
只影响本进程及之后启动的子进程，不写系统或用户永久配置
"""

import os

from notmyfault.plugin_api import native_lock

_ERROR_ENVVAR_NOT_FOUND = 203


def _sync_windows_environment(name, value):
    if os.name != "nt":
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    setter = kernel32.SetEnvironmentVariableW
    setter.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    setter.restype = ctypes.c_int
    if not setter(name, value):
        error = ctypes.get_last_error()
        if value is not None or error != _ERROR_ENVVAR_NOT_FOUND:
            raise ctypes.WinError(error)


def _require_name(params):
    raw = params.get("name", "")
    if not isinstance(raw, str):
        raise ValueError("变量名必须是文本")
    if not raw:
        raise ValueError("变量名不能为空")
    if raw != raw.strip():
        raise ValueError("变量名首尾不能有空格")
    if "=" in raw:
        raise ValueError("变量名不能包含等号")
    if "\0" in raw:
        raise ValueError("变量名不能包含空字符")
    return raw


def _get(name, params):
    missing = params.get("missing", "empty")
    if missing not in ("empty", "error"):
        raise ValueError("变量不存在时只能选择当作空值或报错")
    try:
        value = os.environ[name]
    except KeyError:
        if missing == "error":
            raise ValueError("环境变量不存在") from None
        return {"name": name, "value": "", "existed": False}
    return {"name": name, "value": value, "existed": True}


def _set(name, params):
    value = params.get("value", "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("变量值必须是文本")
    with native_lock():
        existed = name in os.environ
        os.environ[name] = value
        _sync_windows_environment(name, value)
    return {"name": name, "value": value, "existed": existed}


def _unset(name):
    with native_lock():
        try:
            del os.environ[name]
        except KeyError:
            existed = False
        else:
            existed = True
        _sync_windows_environment(name, None)
    return {"name": name, "existed": existed}


def run(action_info, params):
    mode = params.get("mode", "get")
    if mode not in ("get", "set", "unset"):
        raise ValueError("未知操作")
    name = _require_name(params)
    print(f"[Action:environment_variable] {mode} {name}")
    if mode == "get":
        return _get(name, params)
    if mode == "set":
        return _set(name, params)
    return _unset(name)
