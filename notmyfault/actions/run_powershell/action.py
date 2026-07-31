import subprocess
import sys


def run(action_info, params):
    command = params.get("command", "")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("未指定命令")

    command = command.strip()
    print(f"[Action:run_powershell] 执行: {command}")

    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
            )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("命令执行超时（60s）") from e
    except FileNotFoundError as e:
        raise RuntimeError("未找到 PowerShell，请确认已安装") from e
    except OSError as e:
        raise RuntimeError(f"命令执行异常: {e}") from e

    if result.returncode != 0:
        raise RuntimeError(
            f"命令执行失败 (code={result.returncode}): {result.stderr.strip()}"
        )
    print(f"[Action:run_powershell] 执行成功")
    return {"returncode": 0, "stdout": result.stdout, "stderr": result.stderr}
