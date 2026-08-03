"""提供 run_as_admin() 管理员命令入口，插件授权按引擎令牌和调用模块身份检查"""

import inspect
import os
import secrets
import shutil
import subprocess
import sys
import threading
from typing import Any

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
_admin_by_module: dict[int, str] = {}
_session_lock = threading.RLock()


def begin_engine_session(token: str | None = None) -> str:
    """开始一代引擎权限会话并清理上一代令牌和授权"""
    global _engine_token, _admin_plugins, _admin_by_module
    session_token = token or secrets.token_hex(32)
    with _session_lock:
        _engine_token = session_token
        _admin_plugins.clear()
        _admin_by_module.clear()
    return session_token


def end_engine_session(token: str) -> bool:
    """结束匹配的权限会话，旧引擎的令牌无法清理新一代会话"""
    global _engine_token, _admin_plugins, _admin_by_module
    with _session_lock:
        if _engine_token is None or not secrets.compare_digest(token, _engine_token):
            return False
        _engine_token = None
        _admin_plugins.clear()
        _admin_by_module.clear()
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
            _admin_by_module[id(module.__dict__)] = plugin_id


def deauthorize_plugin(plugin_id: str, token: str) -> bool:
    """验证令牌后撤销插件管理员授权并清理模块记录"""
    with _session_lock:
        if _engine_token is None or not secrets.compare_digest(token, _engine_token):
            return False
        removed = plugin_id in _admin_plugins
        _admin_plugins.discard(plugin_id)
        for key in [
            key for key, pid in _admin_by_module.items() if pid == plugin_id
        ]:
            del _admin_by_module[key]
        return removed


def _find_plugin_caller() -> tuple[str | None, Any | None]:
    """从调用栈中返回第一个插件模块的 ID 和全局字典。"""
    try:
        for frame_info in inspect.stack():
            module_name = frame_info.frame.f_globals.get("__name__", "")
            for prefix in ("notmyfault.action_", "notmyfault.trigger_"):
                if module_name.startswith(prefix):
                    return module_name[len(prefix):], frame_info.frame.f_globals
        return None, None
    except Exception:
        return None, None


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


def run_as_admin(
    command: list[str],
    wait: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """以管理员权限执行命令，未授权调用者或空命令会抛出对应异常"""
    if not command:
        raise ValueError("command 不能为空")

    # 授权按模块全局字典身份绑定，同名替换只使用新模块授权。
    caller_id, caller_globals = _find_plugin_caller()
    if caller_globals is None or id(caller_globals) not in _admin_by_module:
        if caller_id is not None:
            _warn_no_admin_permission(caller_id)
            raise PermissionError(
                f"插件 '{caller_id}' 未授权使用 run_as_admin()。"
                f"请在插件元数据的 permissions 字段中添加 \"admin\" 并重启引擎。"
            )
        # 找不到插件命名空间时直接拒绝调用
        raise PermissionError(
            "无法确定 run_as_admin() 的调用者身份，拒绝执行。"
            "请在被引擎授权的插件模块中调用。"
        )

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


# 导入守卫必须在所有定义完成后执行。
_guard_sudo_import()
