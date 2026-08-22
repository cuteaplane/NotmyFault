"""isolated action 的父进程端：起子进程、收结果、管超时和回收

目标是故障隔离，不是安全 sandbox：动作代码在自己的进程里崩，引擎照常活着。
manifest 写 "execution_mode": "isolated" 的 action 走这里，其余仍进进程内执行。
每次执行起一个短命子进程；引擎 shutdown 时 shutdown_all() 把还活着的子进程
terminate 掉。
"""
import subprocess
import sys
import threading

# worker 各阶段的等待上限（秒）：执行结果、退出宽限；解释器启动耗时算进执行超时
EXECUTE_TIMEOUT = 120.0
EXIT_TIMEOUT = 5.0


class PluginWorkerCrashed(RuntimeError):
    """worker 子进程没给出结果就退出，包括段错误和 os._exit"""


class PluginWorkerTimeout(RuntimeError):
    """worker 在执行超时内没有返回结果"""


_lock = threading.Lock()
_live_processes: set[subprocess.Popen] = set()
_on_crash = None


def set_crash_callback(callback) -> None:
    """引擎把 _safe_on_event 挂进来，崩溃时推送 plugin_worker_crashed 事件"""
    global _on_crash
    _on_crash = callback


def _sanitize_context(context):
    """把 context 削成能过 JSON 的形状：下划线开头的内部键和不可序列化对象都去掉"""
    def clean(value):
        if isinstance(value, dict):
            return {
                key: clean(item)
                for key, item in value.items()
                if not str(key).startswith("_")
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)

    return clean(context)


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass


def run_isolated_action(
    entry: str,
    action_info: dict,
    params: dict,
    context: dict,
    execute_timeout: float = EXECUTE_TIMEOUT,
):
    """在子进程里跑一次 action，返回 (ok, result 或错误消息)"""
    request = {
        "entry": entry,
        "action_info": action_info,
        "params": params,
        "context": _sanitize_context(context),
    }
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "notmyfault.core.plugin_worker_main"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with _lock:
        _live_processes.add(process)
    timed_out = threading.Event()

    def watchdog():
        if timed_out.wait(timeout=execute_timeout):
            return
        timed_out.set()
        _stop_process(process)

    timer = threading.Timer(execute_timeout, watchdog)
    timer.daemon = True
    timer.start()
    try:
        return _drive(process, request, execute_timeout, timed_out)
    finally:
        timer.cancel()
        with _lock:
            _live_processes.discard(process)
        _stop_process(process)


def _drive(process, request, execute_timeout, timed_out):
    import json

    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        process.stdin.flush()
    except (OSError, ValueError):
        _report_crash(process, "无法向 worker 写入请求")
        raise PluginWorkerCrashed("无法向 worker 写入请求")

    try:
        line = process.stdout.readline()
    except OSError:
        line = ""
    if timed_out.is_set():
        raise PluginWorkerTimeout(
            f"worker 执行超过 {execute_timeout}s，已终止"
        )
    if not line:
        _report_crash(process, "worker 没有返回结果就退出了")
        raise PluginWorkerCrashed(
            f"worker 异常退出，退出码 {process.poll()}"
        )
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        _report_crash(process, "worker 输出不是 JSON 协议")
        raise PluginWorkerCrashed("worker 输出不是 JSON 协议")

    if payload.get("ok") is True:
        return True, payload.get("result")
    return False, payload.get("error") or "worker 执行失败"


def _report_crash(process, reason: str) -> None:
    callback = _on_crash
    if callback is None:
        return
    # 先等进程收尸拿到退出码，poll() 这时才不是 None
    try:
        returncode = process.wait(timeout=EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        returncode = process.poll()
    try:
        callback(
            "plugin_worker_crashed",
            {"pid": process.pid, "returncode": returncode, "reason": reason},
        )
    except Exception:
        pass


def shutdown_all() -> None:
    """引擎关闭时回收所有还活着的 worker 子进程"""
    with _lock:
        processes = list(_live_processes)
    for process in processes:
        _stop_process(process)
