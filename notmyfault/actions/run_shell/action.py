import os
import select
import shutil
import subprocess
import sys
import threading
import time
import locale

# 命令输出会进规则上下文和日志，全量保留大输出会撑爆内存
_MAX_OUTPUT = 100 * 1024


def _read_available(stream):
    descriptor = stream.fileno()
    return os.read(descriptor, 4096) if select.select([descriptor], [], [], 0)[0] else None


def _shell_executable():
    for name in ("bash", "sh"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("未找到 bash 或 sh")


def run_with_context(action_info, params, context):
    if not sys.platform.startswith("linux"):
        raise RuntimeError("执行 Shell 仅支持 Linux，Windows 请使用 run_powershell")

    command = params.get("command", "")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("未指定命令")

    command = command.strip()
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

    try:
        executable = _shell_executable()
        args = [executable, "-c", command]
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        readers = [
            threading.Thread(target=read_output, args=(stream, result), daemon=True)
            for stream, result in zip((process.stdout, process.stderr), streams)
        ]
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
        raise RuntimeError("未找到 bash 或 sh") from e
    except OSError as e:
        raise RuntimeError(f"命令执行异常: {e}") from e

    if process.returncode != 0:
        raise RuntimeError(f"命令执行失败 (code={process.returncode})")
    if cancellation:
        cancellation.raise_if_cancelled()
    print("[Action:run_shell] 执行成功")
    return {
        "returncode": 0,
        **{
            name: bytes(result["data"]).decode(
                locale.getpreferredencoding(False), errors="replace"
            )
            + ("...（已截断）" if result["truncated"] else "")
            for name, result in zip(("stdout", "stderr"), streams)
        },
    }


def run(action_info, params):
    return run_with_context(action_info, params, {})
