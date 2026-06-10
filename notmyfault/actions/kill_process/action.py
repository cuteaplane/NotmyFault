import psutil


def run(action_info, params):
    process_name = params.get("process_name", "").strip()
    if not process_name:
        print("[Action:kill_process] 未指定进程名，跳过")
        return

    print(f"[Action:kill_process] 正在终止进程: {process_name}")

    killed = 0
    target = process_name.lower()
    if not target.endswith(".exe"):
        target += ".exe"

    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() == target:
                    proc.terminate()
                    killed += 1
                    print(f"[Action:kill_process] 已终止 PID={proc.info['pid']}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if killed == 0:
            print(f"[Action:kill_process] 未找到运行中的进程: {process_name}")
        else:
            print(f"[Action:kill_process] 共终止了 {killed} 个进程")
    except Exception as e:
        print(f"[Action:kill_process] 失败: {e}")
