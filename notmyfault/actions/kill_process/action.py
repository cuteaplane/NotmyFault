import os
import time
import psutil


_PROTECTED_WINDOWS_PROCESSES = frozenset(
    {
        "csrss.exe",
        "dwm.exe",
        "lsass.exe",
        "services.exe",
        "smss.exe",
        "system",
        "wininit.exe",
        "winlogon.exe",
    }
)


def _target_name(process_name: str) -> str:
    target = process_name.lower()
    if os.name == "nt" and not target.endswith(".exe"):
        target += ".exe"
    return target


def run(action_info, params):
    process_name = params.get("process_name", "").strip()
    if not process_name:
        raise ValueError("未指定进程名")

    print(f"[Action:kill_process] 正在终止进程: {process_name}")

    self_pid = os.getpid()
    target = _target_name(process_name)
    if os.name == "nt" and target in _PROTECTED_WINDOWS_PROCESSES:
        raise ValueError("不允许终止 Windows 关键系统进程")
    killed = 0
    denied = 0

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == target:
                pid = proc.info.get("pid")
                if pid == self_pid:
                    print("[Action:kill_process] 跳过引擎自身进程")
                    continue
                proc.terminate()
                try:
        # terminate() 是异步的，等待并验证进程确实退出
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    print(
                        f"[Action:kill_process] PID={pid or '?'} "
                        "5 秒内未退出，发送强杀信号"
                    )
                    proc.kill()
                    proc.wait(timeout=5)
                killed += 1
                print(f"[Action:kill_process] 已终止 PID={pid or '?'}")
        except psutil.AccessDenied:
            denied += 1
            pid = proc.info.get("pid", "?")
            print(
                f"[Action:kill_process] PID={pid} 权限不足，"
                "未终止（可能需管理员权限）"
            )
        except psutil.NoSuchProcess:
            continue

    if killed == 0 and denied:
        # 一个都没杀掉且全被拒，才算失败；杀了一部分时把 denied 带回结果
        raise RuntimeError(f"{denied} 个进程因权限不足未能终止（可能需管理员权限）")
    if denied:
        print(f"[Action:kill_process] 另有 {denied} 个进程因权限不足未能终止")
    if killed == 0:
        # 进程本来就不存在，视为成功
        print(f"[Action:kill_process] 未找到运行中的进程: {process_name}")
        return {"killed": 0, "denied": denied}
    print(f"[Action:kill_process] 共终止了 {killed} 个进程")
    return {"killed": killed, "denied": denied}
