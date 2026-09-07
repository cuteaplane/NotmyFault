import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from notmyfault.core.data_types import DataTypeError, copy_value
from notmyfault.core.value_codec import decode_value, encode_value


STARTUP_TIMEOUT = 10.0
EXECUTE_TIMEOUT = 120.0
EXIT_TIMEOUT = 5.0
PROTOCOL_VERSION = 1
MAX_PROTOCOL_LINE = 1024 * 1024
MAX_STDERR_CHUNK = 64 * 1024
_WORKER_MAIN = Path(__file__).resolve().with_name("plugin_worker_main.py")
_PROJECT_ROOT = _WORKER_MAIN.parents[2]


class PluginWorkerCrashed(RuntimeError):
    """worker 子进程没给出结果就退出，包括段错误和 os._exit"""


class PluginWorkerTimeout(RuntimeError):
    """worker 没在给定时间内返回"""


class PluginWorkerStartupTimeout(PluginWorkerTimeout):
    """worker 没在启动时间内发回 ready"""


_lock = threading.Lock()
_live_processes: set[subprocess.Popen] = set()
_on_crash = None
_PROTOCOL_EOF = object()
_PROTOCOL_OVERSIZE = object()


def set_crash_callback(callback) -> None:
    """引擎把 _safe_on_event 传进来，worker 崩溃时发送事件"""
    global _on_crash
    _on_crash = callback


def _sanitize_context(context):
    """上下文顶层的内部键不进入 worker，业务对象保留原字段名。"""
    def clean(value):
        if isinstance(value, dict):
            return {
                key: clean(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        try:
            return copy_value(value)
        except DataTypeError:
            return repr(value)

    return clean({key: value for key, value in context.items() if not str(key).startswith("_")})


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


def _wait_for_exit(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        _stop_process(process)


def _drain_stderr(stream) -> None:
    target = sys.stderr
    try:
        while True:
            line = stream.readline(MAX_STDERR_CHUNK + 1)
            if not line:
                break
            if len(line) > MAX_STDERR_CHUNK:
                line = line[:MAX_STDERR_CHUNK] + "\n"
            if target is None:
                continue
            try:
                target.write(line)
                target.flush()
            except (OSError, ValueError):
                target = None
    finally:
        stream.close()


def _read_protocol(stream, messages: queue.Queue) -> None:
    try:
        while True:
            line = stream.readline(MAX_PROTOCOL_LINE + 1)
            if not line:
                break
            if len(line) > MAX_PROTOCOL_LINE:
                messages.put(_PROTOCOL_OVERSIZE)
                return
            messages.put(line)
    finally:
        stream.close()
        messages.put(_PROTOCOL_EOF)


def run_isolated_action(
    entry: str,
    entry_sha256: str,
    action_info: dict,
    params: dict,
    context: dict,
    startup_timeout: float = STARTUP_TIMEOUT,
    execute_timeout: float = EXECUTE_TIMEOUT,
    *,
    plugin_root: str | None = None,
    file_snapshot: dict[str, str] | None = None,
):
    from notmyfault.security.plugin_loader import _snapshot_plugin_files

    plugin_root = plugin_root or str(Path(entry).resolve().parent)
    if file_snapshot is None:
        file_snapshot = _snapshot_plugin_files(plugin_root)
    request = {
        "entry": entry,
        "entry_sha256": entry_sha256,
        "plugin_root": plugin_root,
        "file_snapshot": file_snapshot,
        "action_info": action_info,
        "params": params,
        "context": _sanitize_context(context),
        "value_encoding": "typed-v1",
        "data_types": context["_type_registry"].snapshot() if context.get("_type_registry") is not None else {},
    }
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(_WORKER_MAIN)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(_PROJECT_ROOT),
    )
    with _lock:
        _live_processes.add(process)
    if process.stderr is None or process.stdout is None:
        _stop_process(process)
        raise PluginWorkerCrashed("无法连接 worker 输出")
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(process.stderr,),
        daemon=True,
    )
    messages: queue.Queue = queue.Queue(maxsize=4)
    stdout_thread = threading.Thread(
        target=_read_protocol,
        args=(process.stdout, messages),
        daemon=True,
    )
    stderr_thread.start()
    stdout_thread.start()
    completed = False
    started = time.monotonic()
    try:
        startup_budget = min(max(float(startup_timeout), 0.0), execute_timeout)
        _wait_until_ready(process, messages, startup_budget)
        remaining = max(float(execute_timeout) - (time.monotonic() - started), 0.0)
        result = _drive(process, messages, request, remaining)
        completed = True
        return result
    finally:
        with _lock:
            _live_processes.discard(process)
        if completed:
            _wait_for_exit(process)
        else:
            _stop_process(process)
        stdout_thread.join(timeout=EXIT_TIMEOUT)
        stderr_thread.join(timeout=EXIT_TIMEOUT)


def _wait_until_ready(process, messages, startup_timeout):
    payload = _receive_payload(
        process,
        messages,
        startup_timeout,
        PluginWorkerStartupTimeout(
            f"worker 启动超过 {startup_timeout}s，已终止"
        ),
        "worker 没有发回启动结果就退出了",
    )
    if (
        payload.get("type") != "ready"
        or payload.get("protocol") != PROTOCOL_VERSION
    ):
        _fail_protocol(process, "worker 启动结果不符合协议")


def _drive(process, messages, request, execute_timeout):
    try:
        if process.stdin is None:
            raise OSError("stdin unavailable")
        wire_request = encode_value(request) if request.get("value_encoding") == "typed-v1" else request
        process.stdin.write(json.dumps(wire_request, ensure_ascii=False, allow_nan=False) + "\n")
        process.stdin.flush()
        process.stdin.close()
    except (OSError, ValueError):
        _fail_protocol(process, "无法向 worker 写入请求")

    payload = _receive_payload(
        process,
        messages,
        execute_timeout,
        PluginWorkerTimeout(f"worker 执行超过 {execute_timeout}s，已终止"),
        "worker 没有返回结果就退出了",
    )
    if payload.get("type") != "result":
        _fail_protocol(process, "worker 执行结果不符合协议")
    if payload.get("ok") is True:
        result = payload.get("result")
        if payload.get("value_encoding") == "typed-v1":
            try:
                result = decode_value(result)
            except DataTypeError:
                _fail_protocol(process, "worker 返回的数据编码无效")
        return True, result
    return False, payload.get("error") or "worker 执行失败"


def _receive_payload(process, messages, timeout, timeout_error, eof_reason):
    try:
        line = messages.get(timeout=max(0.0, timeout))
    except queue.Empty:
        _stop_process(process)
        _report_crash(process, str(timeout_error))
        raise timeout_error
    if line is _PROTOCOL_EOF:
        _report_crash(process, eof_reason)
        raise PluginWorkerCrashed(
            f"worker 异常退出，退出码 {process.poll()}"
        )
    if line is _PROTOCOL_OVERSIZE:
        _fail_protocol(process, "worker 协议消息超过大小限制")
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        _fail_protocol(process, "worker 输出不是 JSON 协议")
    if not isinstance(payload, dict):
        _fail_protocol(process, "worker 输出不是 JSON 对象")
    return payload


def _fail_protocol(process, reason):
    _stop_process(process)
    _report_crash(process, reason)
    raise PluginWorkerCrashed(reason)


def _report_crash(process, reason: str) -> None:
    callback = _on_crash
    if callback is None:
        return
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
    """引擎关闭时结束所有还活着的 worker 子进程"""
    with _lock:
        processes = list(_live_processes)
    for process in processes:
        _stop_process(process)
