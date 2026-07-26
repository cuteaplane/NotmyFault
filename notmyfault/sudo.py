"""
NotmyFault 提权辅助模块
------------------------
插件应通过此模块请求管理员权限，而不是自己直接调用 PowerShell 提权。

安全机制：
  1. 引擎启动时生成进程级秘密令牌；只有持有该令牌的代码才能注册管理员插件。
  2. run_as_admin() 通过调用栈追踪调用者，只有被引擎显式授权的插件才能提权。
  3. 外部恶意代码即使直接 import sudo.run_as_admin()，也会因未注册而被拒绝。

用法:
    from notmyfault.sudo import run_as_admin

    result = run_as_admin(["net", "start", "MyService"])
    if result.returncode == 0:
        print("操作成功")

这不是沙箱 — 同一进程内的 Python 代码仍然可以绕过它。它的作用是：
1. 提供一条"正道"，让插件开发者无需自己写复杂的 UAC 逻辑
2. 配合插件元数据中的 "permissions": ["admin"] 做声明式权限管理
3. 未来如果需要进程隔离，只需修改这个模块即可
"""

import inspect
import secrets
import subprocess
import sys
import threading

# ---------------------------------------------------------------------------
# 进程级令牌系统
# ---------------------------------------------------------------------------
# _engine_token: 引擎在 __init__ 时设置，外部代码无法知晓
# _admin_plugins: 被引擎信任的插件 ID 集合（必须在 metadata 中声明 admin 权限）

_engine_token: str | None = None
_admin_plugins: set[str] = set()
_session_lock = threading.RLock()


def begin_engine_session(token: str | None = None) -> str:
    """开始一代引擎权限会话，并撤销上一代遗留的插件授权。

    RuntimeController 保证同一时刻只有一代引擎运行；这里仍以原子轮换兜底，
    避免后台进程内停止后重启时沿用旧令牌和旧授权集合。
    """
    global _engine_token, _admin_plugins
    session_token = token or secrets.token_hex(32)
    with _session_lock:
        _engine_token = session_token
        _admin_plugins.clear()
    return session_token


def end_engine_session(token: str) -> bool:
    """结束匹配的权限会话；旧引擎不能撤销新一代会话。"""
    global _engine_token, _admin_plugins
    with _session_lock:
        if _engine_token is None or not secrets.compare_digest(token, _engine_token):
            return False
        _engine_token = None
        _admin_plugins.clear()
        return True


def set_engine_token(token: str) -> None:
    """兼容旧调用：以指定令牌开始一代新权限会话。"""
    begin_engine_session(token)


def authorize_plugin(plugin_id: str, token: str) -> None:
    """授权插件使用管理员权限。

    只能在引擎设置令牌后调用，且 token 必须匹配引擎令牌。
    由引擎在加载声明了 'admin' 权限的插件时调用。
    """
    with _session_lock:
        if _engine_token is None:
            raise RuntimeError("引擎令牌尚未设置，无法授权插件")
        if not secrets.compare_digest(token, _engine_token):
            raise PermissionError("令牌不匹配，拒绝授权")
        _admin_plugins.add(plugin_id)


def _get_caller_plugin_id() -> str | None:
    """遍历调用栈，找到调用 run_as_admin() 的插件模块 ID。

    检测规则：调用栈中第一个属于 notmyfault.action_ 或 notmyfault.trigger_
    前缀的模块，提取其插件 ID。
    """
    try:
        for frame_info in inspect.stack():
            module_name = frame_info.frame.f_globals.get("__name__", "")
            for prefix in ("notmyfault.action_", "notmyfault.trigger_"):
                if module_name.startswith(prefix):
                    return module_name[len(prefix):]
        return None
    except Exception:
        return None


def is_authorized(plugin_id: str) -> bool:
    """检查插件是否已被授权管理员权限。"""
    with _session_lock:
        return plugin_id in _admin_plugins


def get_authorized_plugins() -> list[str]:
    """返回当前已授权的插件 ID 列表（仅供诊断使用）。"""
    with _session_lock:
        return sorted(_admin_plugins)


def _warn_no_admin_permission(plugin_id: str) -> None:
    """插件未声明 admin 权限但尝试提权时告警。"""
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
    """以管理员权限执行命令。

    安全校验：
      1. 调用者必须是已通过 authorize_plugin() 注册的管理员插件。
      2. 如果调用者未注册，抛出 PermissionError 并记录告警。

    Args:
        command: 命令和参数列表，如 ["net", "start", "MyService"]
        wait: 是否等待命令执行完成（默认 True）
        timeout: 超时秒数（默认 30）

    Returns:
        subprocess.CompletedProcess 对象；如果 wait=False，返回的
        CompletedProcess.returncode 为 0（不保证实际执行结果）。

    Raises:
        PermissionError: 调用者未授权管理员权限。
        ValueError: command 为空。
    """
    if not command:
        raise ValueError("command 不能为空")

    # --- 权限校验 ---
    caller_id = _get_caller_plugin_id()
    if caller_id is not None and not is_authorized(caller_id):
        _warn_no_admin_permission(caller_id)
        raise PermissionError(
            f"插件 '{caller_id}' 未授权使用 run_as_admin()。"
            f"请在插件元数据的 permissions 字段中添加 \"admin\" 并重启引擎。"
        )
    # 如果无法检测调用者，拒绝执行（PoC-6 修复）：
    # 非插件命名空间的调用（恶意脚本/第三方包/exec 绕过后间接调用）
    # caller_id=None，之前放行，现在拒绝以防绕过提权。
    if caller_id is None:
        raise PermissionError(
            "无法确定 run_as_admin() 的调用者身份，拒绝执行。"
            "请在被引擎授权的插件模块中调用。"
        )

    # 安全转义：单引号内 '' 表示一个字面单引号
    def _ps_quote(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    exe = _ps_quote(command[0])
    args = ", ".join(_ps_quote(a) for a in command[1:])
    ps_script = (
        f"Start-Process -FilePath {exe}"
        + (f" -ArgumentList {args}" if args else "")
        + " -Verb RunAs"
        + (" -Wait" if wait else "")
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
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
