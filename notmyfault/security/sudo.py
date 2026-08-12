"""提供 run_as_admin() 管理员命令入口，插件授权按引擎令牌和调用模块身份检查"""

import inspect
import os
import secrets
import shutil
import subprocess
import sys
import threading
import uuid
import weakref
from typing import Any

from notmyfault.security.errors import AdminExecutionBlocked

# strict 模式只允许插件命名空间和引擎核心导入 sudo，引擎核心负责令牌与授权
_ENGINE_CORE_MODULES = {"notmyfault.core.engine"}


def _guard_sudo_import() -> None:
    """校验 sudo 模块的导入者身份，在模块加载时执行一次"""
    # 打包后的冻结程序由打包环境控制，跳过导入者检查
    if getattr(sys, "frozen", False):
        return
    try:
        caller_name = None
        for frame_info in inspect.stack():
            name = frame_info.frame.f_globals.get("__name__", "")
            if (
                name == "notmyfault.security.sudo"
                or name.startswith("importlib")
                or name.startswith("_frozen_importlib")
            ):
                continue
            caller_name = name
            break
    except Exception:
        return
    if caller_name is None:
        return
    # 插件命名空间和引擎核心是合法导入者，其他 notmyfault 组件在 strict 模式拒绝。
    if (
        caller_name.startswith("notmyfault.action_")
        or caller_name.startswith("notmyfault.trigger_")
        or caller_name in _ENGINE_CORE_MODULES
    ):
        return
    if caller_name.startswith("notmyfault"):
        caller_kind = f"notmyfault 组件 {caller_name}"
    elif (
        "pytest" in sys.modules
        or "_pytest" in sys.modules
        or _is_project_script()
    ):
        return
    else:
        caller_kind = f"外部代码 {caller_name}"
    from notmyfault.security.security import detect_security_mode
    mode = detect_security_mode()
    if mode is not None and mode.value != "strict":
        print(
            f"[sudo] 宽松模式（{mode.value}）：{caller_kind} 导入 notmyfault.security.sudo，放行",
            file=sys.stderr,
        )
        return
    raise ImportError(
        f"notmyfault.security.sudo 安全限制（strict）：仅插件（notmyfault.action_*/trigger_*）"
        f"与引擎核心可导入，{caller_kind} 被拒绝。"
        f"插件请声明 'admin' 权限后通过 run_as_admin 提权。"
    )


def _is_project_script() -> bool:
    """项目根目录内的官方入口和测试脚本视为可信。"""
    try:
        for frame_info in inspect.stack():
            file_path = frame_info.frame.f_globals.get("__file__") or ""
            if not file_path:
                continue
            real = os.path.realpath(file_path)
            root = os.path.realpath(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            )
            if os.path.commonpath([real, root]) == root:
                return True
    except Exception:
        pass
    return False


# 引擎令牌由会话设置，运行时授权按模块命名空间身份保存供诊断查询

_engine_token: str | None = None
_admin_plugins: set[str] = set()
_admin_by_module: dict[int, tuple[weakref.ReferenceType, str]] = {}
_authorization_mode = "direct"
_admin_broker = None
_session_lock = threading.RLock()

ADMIN_AUTHORIZATION_MODES = {"per_execution", "engine_start"}


def _broker_is_active(broker) -> bool:
    return broker is not None and getattr(broker, "active", True)


class _AdminBrokerClient:
    """持有一条只连接已提升进程的本代引擎命名管道。"""

    def __init__(self, handle) -> None:
        self._handle = handle
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._handle is not None

    @staticmethod
    def _pipe_client_is_elevated(handle) -> bool:
        import win32api
        import win32con
        import win32pipe
        import win32security

        process = token = None
        try:
            pid = win32pipe.GetNamedPipeClientProcessId(handle)
            process = win32api.OpenProcess(
                getattr(win32con, "PROCESS_QUERY_LIMITED_INFORMATION", 0x1000),
                False,
                pid,
            )
            token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
            return bool(
                win32security.GetTokenInformation(
                    token, win32security.TokenElevation
                )
            )
        except Exception:
            return False
        finally:
            for item in (token, process):
                if item is not None:
                    try:
                        item.Close()
                    except Exception:
                        pass

    @classmethod
    def start(cls, timeout: float = 60.0) -> "_AdminBrokerClient":
        if os.name != "nt":
            raise RuntimeError("启动时一次授权目前只支持 Windows")

        import ctypes
        import win32file
        import win32pipe

        from notmyfault.security.admin_broker import read_packet

        pipe_name = rf"\\.\pipe\NotmyFaultAdmin-{uuid.uuid4().hex}"
        handle = win32pipe.CreateNamedPipe(
            pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1,
            65536,
            65536,
            0,
            None,
        )

        if getattr(sys, "frozen", False):
            arguments = ["--admin-broker", pipe_name]
        else:
            entry_path = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "NOTMYFAULT.pyw")
            )
            arguments = [entry_path, "--admin-broker", pipe_name]
        parameters = subprocess.list2cmdline(arguments)
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            parameters,
            os.path.dirname(os.path.abspath(__file__)),
            0,
        )
        if int(result) <= 32:
            win32file.CloseHandle(handle)
            raise PermissionError("管理员授权已取消或 UAC 启动失败")

        connected = threading.Event()
        outcome: dict[str, Any] = {}

        def accept_broker() -> None:
            try:
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except Exception as error:
                    if getattr(error, "winerror", None) != 535:
                        raise
                ready = read_packet(handle)
                if ready.get("op") != "ready":
                    raise PermissionError("管理员代理握手无效")
                pipe_pid = win32pipe.GetNamedPipeClientProcessId(handle)
                if ready.get("pid") != pipe_pid or not cls._pipe_client_is_elevated(handle):
                    raise PermissionError("管理员代理未通过提升令牌校验")
                outcome["handle"] = handle
            except Exception as error:
                outcome["error"] = error
            finally:
                connected.set()

        threading.Thread(
            target=accept_broker,
            name="AdminBrokerConnect",
            daemon=True,
        ).start()
        if not connected.wait(timeout=max(float(timeout), 0.1)):
            win32file.CloseHandle(handle)
            raise TimeoutError("等待管理员授权超时")
        if "error" in outcome:
            win32file.CloseHandle(handle)
            raise RuntimeError(f"管理员代理连接失败: {outcome['error']}")
        return cls(outcome["handle"])

    def execute(
        self,
        command: list[str],
        wait: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess:
        from notmyfault.security.admin_broker import read_packet, write_packet

        with self._lock:
            if self._handle is None:
                raise RuntimeError("管理员授权会话已关闭")
            try:
                write_packet(
                    self._handle,
                    {
                        "op": "execute",
                        "command": command,
                        "wait": wait,
                        "timeout": timeout,
                    },
                )
                response = read_packet(self._handle)
            except Exception:
                handle, self._handle = self._handle, None
                try:
                    import win32file
                    win32file.CloseHandle(handle)
                except Exception:
                    pass
                raise
        if response.get("ok") is not True:
            if response.get("error") == "timeout":
                raise subprocess.TimeoutExpired(command, timeout)
            raise RuntimeError(response.get("error") or "管理员代理执行失败")
        return subprocess.CompletedProcess(
            command,
            int(response.get("returncode", 1)),
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
        )

    def close(self) -> None:
        import win32file

        from notmyfault.security.admin_broker import read_packet, write_packet

        with self._lock:
            handle, self._handle = self._handle, None
            if handle is None:
                return
            try:
                write_packet(handle, {"op": "shutdown"})
                read_packet(handle)
            except Exception:
                pass
            finally:
                try:
                    win32file.CloseHandle(handle)
                except Exception:
                    pass


def begin_engine_session(
    token: str | None = None,
    authorization_mode: str | None = None,
) -> str:
    """开始一代引擎权限会话并清理上一代令牌和授权"""
    global _engine_token, _admin_plugins, _admin_by_module
    global _authorization_mode, _admin_broker
    if authorization_mode is None:
        authorization_mode = "direct"
    elif authorization_mode not in ADMIN_AUTHORIZATION_MODES:
        authorization_mode = "per_execution"
    session_token = token or secrets.token_hex(32)
    with _session_lock:
        previous_broker = _admin_broker
        _admin_broker = None
        _engine_token = session_token
        _authorization_mode = authorization_mode
        _admin_plugins.clear()
        _admin_by_module.clear()
    if previous_broker is not None:
        previous_broker.close()
    return session_token


def end_engine_session(token: str) -> bool:
    """结束匹配的权限会话，旧引擎的令牌无法清理新一代会话"""
    global _engine_token, _admin_plugins, _admin_by_module, _admin_broker
    with _session_lock:
        if _engine_token is None or not secrets.compare_digest(token, _engine_token):
            return False
        broker = _admin_broker
        _admin_broker = None
        _engine_token = None
        _admin_plugins.clear()
        _admin_by_module.clear()
    if broker is not None:
        broker.close()
    return True


def set_engine_token(token: str) -> None:
    """以指定令牌开始一代新权限会话。"""
    begin_engine_session(token)


def authorize_plugin(plugin_id: str, token: str, module=None) -> None:
    """验证令牌后授权插件模块使用管理员权限"""
    with _session_lock:
        if _engine_token is None:
            raise RuntimeError("引擎令牌尚未设置，无法授权插件")
        if not secrets.compare_digest(token, _engine_token):
            raise PermissionError("令牌不匹配，拒绝授权")
        _admin_plugins.add(plugin_id)
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


def get_authorization_status() -> dict[str, Any]:
    """返回本代引擎的管理员授权方式和代理状态。"""
    with _session_lock:
        return {
            "mode": _authorization_mode,
            "session_active": _broker_is_active(_admin_broker),
        }


def start_admin_session(token: str, timeout: float = 60.0) -> bool:
    """在 UAC 通过后启动本代引擎专用的管理员代理。"""
    global _admin_broker
    with _session_lock:
        if _engine_token is None or not secrets.compare_digest(token, _engine_token):
            raise PermissionError("令牌不匹配，拒绝启动管理员授权会话")
        if _authorization_mode != "engine_start":
            return False
        if _broker_is_active(_admin_broker):
            return True

    broker = _AdminBrokerClient.start(timeout=timeout)
    with _session_lock:
        if _engine_token is None or not secrets.compare_digest(token, _engine_token):
            broker.close()
            raise PermissionError("引擎权限会话已结束")
        _admin_broker = broker
    return True


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
    wait: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    if os.name == "nt":
        # PowerShell 单引号使用两个单引号转义。
        def _ps_quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        executable = _ps_quote(command[0])
        arguments = ", ".join(_ps_quote(argument) for argument in command[1:])
        ps_script = (
            f"Start-Process -FilePath {executable}"
            + (f" -ArgumentList {arguments}" if arguments else "")
            + " -Verb RunAs"
            + (" -Wait" if wait else "")
        )
        elevated_command = ["powershell", "-NoProfile", "-Command", ps_script]
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
        if result.returncode != 0 and result.stderr:
            print(
                f"[sudo] 命令执行可能失败 (rc={result.returncode}): "
                f"{result.stderr.strip()}",
                file=sys.stderr,
            )
        return result
    except subprocess.TimeoutExpired:
        print(
            f"[sudo] 命令超时 ({timeout}s): {' '.join(command)}",
            file=sys.stderr,
        )
        raise
    except Exception as e:
        print(
            f"[sudo] 命令执行异常: {e}",
            file=sys.stderr,
        )
        raise


def run_as_admin(
    command: list[str],
    wait: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """按当前授权方式执行管理员命令。"""
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
        mode = _authorization_mode
        broker = _admin_broker
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

    confirm_required = mode == "per_execution"
    if mode == "engine_start":
        if _broker_is_active(broker):
            try:
                return broker.execute(command, wait, timeout)
            except Exception as error:
                raise AdminExecutionBlocked(
                    f"管理员代理连接已中断，请重启引擎：{error}"
                ) from error
        # 热加载新增管理员规则时，本代引擎还没有管理员代理。
        print(
            "[sudo] engine_start 授权会话不可用，降级为单次确认提权",
            file=sys.stderr,
        )
        from notmyfault.core.logging import engine_warn
        engine_warn(
            f"admin_session_fallback: {authorized_id} engine_start broker 缺失，"
            "降级 per_execution"
        )
        confirm_required = True

    if confirm_required and os.name == "nt":
        from notmyfault.security.admin_prompt import confirm_admin_request

        if not confirm_admin_request(authorized_id):
            raise AdminExecutionBlocked("用户未确认本次管理员执行请求")
    return _run_with_uac(command, wait=wait, timeout=timeout)


# 导入守卫必须在所有定义完成后执行。
_guard_sudo_import()
