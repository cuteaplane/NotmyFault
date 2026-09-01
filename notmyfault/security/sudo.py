"""提供 run_as_admin() 管理员命令入口，插件授权按引擎令牌和调用模块身份检查"""

import inspect
import os
import secrets
import shutil
import subprocess
import sys
import threading
import weakref
from typing import Any

from notmyfault.security.errors import AdminExecutionBlocked

_engine_token: str | None = None
_admin_plugins: set[str] = set()
_admin_executables: dict[str, set[str]] = {}
_admin_by_module: dict[int, tuple[weakref.ReferenceType, str]] = {}
_session_lock = threading.RLock()


def _admin_executable_name(
    command: list[str],
    allowed_executables: set[str],
) -> str:
    if (
        not command
        or any(not isinstance(item, str) or "\x00" in item for item in command)
    ):
        raise ValueError("管理员命令格式无效")
    raw_executable = command[0]
    if "/" in raw_executable or "\\" in raw_executable:
        raise PermissionError("管理员命令只能使用清单声明的可执行文件名")
    executable = raw_executable.lower()
    if executable not in allowed_executables:
        raise PermissionError("管理员命令不在允许列表")
    return executable


def _resolve_admin_command(
    command: list[str],
    allowed_executables: set[str],
) -> list[str]:
    executable = _admin_executable_name(command, allowed_executables)
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        if executable == "powershell.exe":
            resolved = os.path.join(
                system_root,
                "System32",
                "WindowsPowerShell",
                "v1.0",
                executable,
            )
        else:
            resolved = os.path.join(system_root, "System32", executable)
    else:
        trusted_path = os.pathsep.join(
            ("/usr/sbin", "/usr/bin", "/sbin", "/bin")
        )
        resolved = shutil.which(executable, path=trusted_path)
        if resolved is None:
            raise FileNotFoundError(f"找不到受信任的管理员命令: {executable}")
        resolved = os.path.realpath(resolved)
    return [resolved, *command[1:]]


def begin_engine_session(
    token: str | None = None,
) -> str:
    """开始一代引擎权限会话并清理上一代令牌和授权"""
    global _engine_token, _admin_plugins, _admin_executables, _admin_by_module
    session_token = token or secrets.token_hex(32)
    with _session_lock:
        _engine_token = session_token
        _admin_plugins.clear()
        _admin_executables.clear()
        _admin_by_module.clear()
    return session_token


def end_engine_session(token: str) -> bool:
    """结束匹配的权限会话，旧引擎的令牌无法清理新一代会话"""
    global _engine_token, _admin_plugins, _admin_executables, _admin_by_module
    with _session_lock:
        if _engine_token is None or not secrets.compare_digest(token, _engine_token):
            return False
        _engine_token = None
        _admin_plugins.clear()
        _admin_executables.clear()
        _admin_by_module.clear()
    return True


def set_engine_token(token: str) -> None:
    """以指定令牌开始一代新权限会话。"""
    begin_engine_session(token)


def authorize_plugin(
    plugin_id: str,
    token: str,
    module=None,
    allowed_executables: set[str] | None = None,
) -> None:
    """验证令牌后授权插件模块使用管理员权限"""
    with _session_lock:
        if _engine_token is None:
            raise RuntimeError("引擎令牌尚未设置，无法授权插件")
        if not secrets.compare_digest(token, _engine_token):
            raise PermissionError("令牌不匹配，拒绝授权")
        _admin_plugins.add(plugin_id)
        _admin_executables[plugin_id] = {
            name.lower() for name in (allowed_executables or set())
        }
        if module is not None:
            module_key = id(module.__dict__)

            def remove_module(reference, key=module_key):
                with _session_lock:
                    current = _admin_by_module.get(key)
                    if current is not None and current[0] is reference:
                        _admin_by_module.pop(key, None)

            _admin_by_module[module_key] = (
                weakref.ref(module, remove_module),
                plugin_id,
            )


def deauthorize_plugin(plugin_id: str, token: str) -> bool:
    """验证令牌后撤销插件管理员授权并清理模块记录"""
    with _session_lock:
        if _engine_token is None or not secrets.compare_digest(token, _engine_token):
            return False
        removed = plugin_id in _admin_plugins
        _admin_plugins.discard(plugin_id)
        _admin_executables.pop(plugin_id, None)
        for key in [
            key
            for key, (_module_ref, pid) in _admin_by_module.items()
            if pid == plugin_id
        ]:
            del _admin_by_module[key]
        return removed


def _plugin_id_for_globals(plugin_globals: Any) -> str | None:
    """只返回仍指向当前模块全局字典的授权记录"""
    key = id(plugin_globals)
    with _session_lock:
        entry = _admin_by_module.get(key)
        if entry is None:
            return None
        module_ref, plugin_id = entry
        module = module_ref()
        if module is None or module.__dict__ is not plugin_globals:
            if _admin_by_module.get(key) is entry:
                _admin_by_module.pop(key, None)
            return None
        return plugin_id


def _find_plugin_caller() -> tuple[str | None, Any | None]:
    """从调用栈中返回第一个插件模块的 ID 和全局字典。"""
    callers = _find_plugin_callers()
    return callers[0] if callers else (None, None)


def _find_plugin_callers() -> list[tuple[str, Any]]:
    """返回调用栈中的全部插件模块及其全局字典。"""
    callers: list[tuple[str, Any]] = []
    seen_globals: set[int] = set()
    try:
        for frame_info in inspect.stack():
            module_name = frame_info.frame.f_globals.get("__name__", "")
            for prefix in ("notmyfault.action_", "notmyfault.trigger_"):
                if module_name.startswith(prefix):
                    globals_id = id(frame_info.frame.f_globals)
                    if globals_id not in seen_globals:
                        callers.append((module_name[len(prefix):], frame_info.frame.f_globals))
                        seen_globals.add(globals_id)
                    break
        return callers
    except Exception:
        return []


def _get_caller_plugin_id() -> str | None:
    """返回调用 run_as_admin() 的插件模块 ID"""
    caller_id, _caller_globals = _find_plugin_caller()
    return caller_id


def is_authorized(plugin_id: str) -> bool:
    """检查插件是否已被授权管理员权限"""
    with _session_lock:
        return plugin_id in _admin_plugins


def get_authorized_plugins() -> list[str]:
    """返回当前已授权的插件 ID 列表供诊断使用"""
    with _session_lock:
        return sorted(_admin_plugins)


def _warn_no_admin_permission(plugin_id: str) -> None:
    """插件未声明 admin 权限但尝试提权时告警"""
    msg = (
        f"[sudo] 安全告警: 插件 '{plugin_id}' 尝试以管理员权限执行命令，"
        f"但该插件未在元数据中声明 'admin' 权限。"
        f"如需授权，请在 action.json/trigger.json 的 permissions 字段中加入 \"admin\"。"
    )
    print(msg, file=sys.stderr)


def _run_with_uac(
    command: list[str],
    allowed_executables: set[str],
    wait: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    command = _resolve_admin_command(command, allowed_executables)
    if os.name == "nt":
        # PowerShell 单引号使用两个单引号转义。
        def _ps_quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        executable = _ps_quote(command[0])
        arguments = ", ".join(_ps_quote(argument) for argument in command[1:])
        start_process = (
            f"Start-Process -FilePath {executable}"
            + (f" -ArgumentList {arguments}" if arguments else "")
            + " -Verb RunAs"
        )
        if wait:
            ps_script = (
                f"$process = {start_process} -Wait -PassThru; "
                "exit $process.ExitCode"
            )
        else:
            ps_script = start_process
        powershell = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        )
        elevated_command = [powershell, "-NoProfile", "-Command", ps_script]
    else:
        pkexec = shutil.which("pkexec")
        if not pkexec:
            raise RuntimeError(
                "当前 Linux 系统缺少 pkexec，无法弹出桌面管理员认证窗口。"
                "请安装 polkit/policykit-1。"
            )
        elevated_command = [pkexec, "--", *command]

    try:
        if not wait:
            subprocess.Popen(
                elevated_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
            return subprocess.CompletedProcess(elevated_command, 0)

        result = subprocess.run(
            elevated_command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        if result.returncode != 0:
            print(
                f"[sudo] 管理员命令执行失败 (rc={result.returncode})",
                file=sys.stderr,
            )
        return result
    except subprocess.TimeoutExpired:
        print(
            f"[sudo] 管理员命令超时 ({timeout}s)",
            file=sys.stderr,
        )
        raise
    except Exception:
        print("[sudo] 管理员命令执行异常", file=sys.stderr)
        raise


def run_as_admin(
    command: list[str],
    wait: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """确认本次请求后执行管理员命令。"""
    if not command:
        raise ValueError("command 不能为空")

    callers = _find_plugin_callers()
    caller_id = callers[0][0] if callers else None
    with _session_lock:
        authorized_callers = [
            (plugin_id, _plugin_id_for_globals(plugin_globals))
            for plugin_id, plugin_globals in callers
        ]
        unauthorized = next(
            (
                plugin_id
                for plugin_id, authorized_id in authorized_callers
                if authorized_id is None or authorized_id not in _admin_plugins
            ),
            None,
        )
        authorized_id = next(
            (
                authorized_id
                for _plugin_id, authorized_id in authorized_callers
                if authorized_id is not None and authorized_id in _admin_plugins
            ),
            None,
        )
        allowed_executables = set(_admin_executables.get(authorized_id, set()))
    if unauthorized is not None:
        _warn_no_admin_permission(unauthorized)
        raise PermissionError(
            f"插件 '{unauthorized}' 未授权使用 run_as_admin()。"
            f"请在插件元数据的 permissions 字段中添加 \"admin\" 并重启引擎。"
        )
    if authorized_id is None:
        if caller_id is not None:
            _warn_no_admin_permission(caller_id)
            raise PermissionError(
                f"插件 '{caller_id}' 未授权使用 run_as_admin()。"
                f"请在插件元数据的 permissions 字段中添加 \"admin\" 并重启引擎。"
            )
        raise PermissionError(
            "无法确定 run_as_admin() 的调用者身份，拒绝执行。"
            "请在被引擎授权的插件模块中调用。"
        )

    try:
        executable = _admin_executable_name(command, allowed_executables)
    except PermissionError as error:
        raise PermissionError(
            f"插件 '{authorized_id}' 未获准执行 {command[0]!r}"
        ) from error

    if os.name == "nt":
        from notmyfault.security.admin_prompt import confirm_admin_request

        if not confirm_admin_request(authorized_id, executable):
            raise AdminExecutionBlocked("用户未确认本次管理员执行请求")

    return _run_with_uac(
        command,
        allowed_executables,
        wait=wait,
        timeout=timeout,
    )
