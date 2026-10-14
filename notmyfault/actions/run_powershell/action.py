from notmyfault.plugin_api import action_values
import os
import subprocess
import sys
import threading
import time

# 命令输出会进规则上下文和日志，全量保留大输出会撑爆内存
_MAX_OUTPUT = 100 * 1024
# 报错时带上的 stderr 摘要长度，太长会把错误消息撑得没法看
_MAX_ERROR_DETAIL = 500
_PEEK_NAMED_PIPE = None


def _read_available(stream):
    descriptor = stream.fileno()
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        global _PEEK_NAMED_PIPE
        if _PEEK_NAMED_PIPE is None:
            peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
            peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                             ctypes.POINTER(wintypes.DWORD)]
            peek.restype = wintypes.BOOL
            _PEEK_NAMED_PIPE = peek
        available = wintypes.DWORD()
        if not _PEEK_NAMED_PIPE(msvcrt.get_osfhandle(descriptor), None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() == 109:
                return b""
            raise ctypes.WinError(ctypes.get_last_error())
        return os.read(descriptor, min(4096, available.value)) if available.value else None
    import select

    return os.read(descriptor, 4096) if select.select([descriptor], [], [], 0)[0] else None


def run_with_context(action_info, params, context):
    params = action_values(params)
    command = params.get("command", "")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("未指定命令")

    cancellation = context.get("runtime", {}).get("cancellation")
    if cancellation:
        cancellation.raise_if_cancelled()
    deadline = time.monotonic() + 60
    streams = [{"data": bytearray(), "truncated": False} for _ in range(2)]
    stop_readers = threading.Event()
    process_exited = threading.Event()
    def read_output(stream, result):
        try:
            while not stop_readers.is_set():
                if time.monotonic() >= deadline or cancellation and cancellation.is_cancelled():
                    result["truncated"] = True
                    break
                chunk = _read_available(stream)
                if chunk is None:
                    if process_exited.is_set():
                        break
                    stop_readers.wait(0.01)
                    continue
                if not chunk:
                    break
                remaining = _MAX_OUTPUT - len(result["data"])
                result["data"].extend(chunk[:remaining])
                result["truncated"] |= len(chunk) > remaining
        except OSError as error:
            result["error"] = str(error)
        finally:
            stream.close()
    if sys.platform != "win32":
        raise RuntimeError("执行 PowerShell 仅支持 Windows，Linux 请使用 run_shell")

    encoding = params.get("encoding", "oem")
    try:
        b"\0".decode(encoding, errors="replace")
    except (LookupError, TypeError, UnicodeError):
        raise ValueError("命令输出编码必须是可用的文本编码，例如 oem、utf-8 或 gb18030") from None

    try:
        powershell = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        )
        args = [powershell, "-NoProfile", "-NonInteractive", "-Command", command]
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        readers = [threading.Thread(target=read_output, args=(stream, result), daemon=True)
                   for stream, result in zip((process.stdout, process.stderr), streams)]
        for reader in readers:
            reader.start()
        try:
            while process.poll() is None:
                if cancellation:
                    cancellation.raise_if_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, 60)
                try:
                    process.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process_exited.set()
            for reader, result in zip(readers, streams):
                reader.join(timeout=max(0, deadline - time.monotonic()))
                if reader.is_alive():
                    result["truncated"] = True
            stop_readers.set()
            for reader in readers:
                reader.join()
        if any(result.get("error") for result in streams):
            raise RuntimeError("读取命令输出失败")
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("命令执行超时（60s）") from e
    except FileNotFoundError as e:
        raise RuntimeError("未找到 PowerShell，请确认已安装") from e
    except OSError as e:
        raise RuntimeError(f"命令执行异常: {e}") from e

    if process.returncode != 0:
        stderr_text = bytes(streams[1]["data"]).decode(
            encoding, errors="replace"
        ).strip()
        if len(stderr_text) > _MAX_ERROR_DETAIL:
            stderr_text = "..." + stderr_text[-_MAX_ERROR_DETAIL:]
        message = f"命令执行失败 (code={process.returncode})"
        if stderr_text:
            message = f"{message}: {stderr_text}"
        raise RuntimeError(message)
    if cancellation:
        cancellation.raise_if_cancelled()
    print("[Action:run_powershell] 执行成功")
    return {
        "returncode": 0,
        **{name: bytes(result["data"]).decode(encoding, errors="replace")
           + ("...（已截断）" if result["truncated"] else "")
           for name, result in zip(("stdout", "stderr"), streams)},
    }
