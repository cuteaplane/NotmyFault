"""
NotmyFault 提权辅助模块
------------------------
插件应通过此模块请求管理员权限，而不是自己直接调用 PowerShell 提权。

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

import subprocess
import sys


def run_as_admin(
    command: list[str],
    wait: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """以管理员权限执行命令。

    Args:
        command: 命令和参数列表，如 ["net", "start", "MyService"]
        wait: 是否等待命令执行完成（默认 True）
        timeout: 超时秒数（默认 30）

    Returns:
        subprocess.CompletedProcess 对象；如果 wait=False，返回的
        CompletedProcess.returncode 为 0（不保证实际执行结果）。
    """
    if not command:
        raise ValueError("command 不能为空")

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
