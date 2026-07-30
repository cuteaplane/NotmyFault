import subprocess
import sys


def run(action_info, params):
    command = params.get("command", "").strip()
    if not command:
        print("[Action:run_powershell] 未指定命令，跳过")
        return

    print(f"[Action:run_powershell] 执行: {command}")

    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60
            )
        else:
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60
            )

        if result.returncode == 0:
            print(f"[Action:run_powershell] 执行成功")
        else:
            print(f"[Action:run_powershell] 执行失败 (code={result.returncode}): {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        print("[Action:run_powershell] 命令执行超时（60s）")
    except FileNotFoundError:
        print("[Action:run_powershell] 未找到 PowerShell，请确认已安装")
    except Exception as e:
        print(f"[Action:run_powershell] 执行异常: {e}")
