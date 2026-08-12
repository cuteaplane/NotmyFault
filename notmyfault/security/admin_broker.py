"""运行一代引擎专用的 Windows 管理员命令代理。"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from typing import Any


_MAX_PACKET_SIZE = 8 * 1024 * 1024
_MAX_OUTPUT_SIZE = 2 * 1024 * 1024


def _output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        value = str(value)
    return value[-_MAX_OUTPUT_SIZE:]


def _read_exact(handle, size: int) -> bytes:
    import win32file

    chunks = []
    remaining = size
    while remaining:
        _status, chunk = win32file.ReadFile(handle, remaining)
        if not chunk:
            raise EOFError("管理员代理连接已关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_packet(handle) -> dict[str, Any]:
    size = struct.unpack("!I", _read_exact(handle, 4))[0]
    if size > _MAX_PACKET_SIZE:
        raise ValueError("管理员代理消息过大")
    payload = json.loads(_read_exact(handle, size).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("管理员代理消息必须是对象")
    return payload


def write_packet(handle, payload: dict[str, Any]) -> None:
    import win32file

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > _MAX_PACKET_SIZE:
        raise ValueError("管理员代理消息过大")
    win32file.WriteFile(handle, struct.pack("!I", len(body)) + body)


def _execute(request: dict[str, Any]) -> dict[str, Any]:
    command = request.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) or "\x00" in item for item in command)
    ):
        return {"ok": False, "error": "管理员命令格式无效"}

    wait = request.get("wait", True) is not False
    timeout = request.get("timeout", 30)
    try:
        timeout = max(float(timeout), 0.1)
    except (TypeError, ValueError):
        timeout = 30.0

    try:
        if not wait:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        return {
            "ok": True,
            "returncode": result.returncode,
            "stdout": _output_text(result.stdout),
            "stderr": _output_text(result.stderr),
        }
    except subprocess.TimeoutExpired as error:
        return {
            "ok": False,
            "error": "timeout",
            "stdout": _output_text(error.stdout),
            "stderr": _output_text(error.stderr),
        }
    except Exception as error:
        return {"ok": False, "error": str(error)}


def run(pipe_name: str) -> int:
    """连接普通权限引擎并处理命令，直到引擎关闭本代会话。"""
    if os.name != "nt":
        return 2

    import win32file
    import win32pipe

    try:
        win32pipe.WaitNamedPipe(pipe_name, 60_000)
        handle = win32file.CreateFile(
            pipe_name,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0,
            None,
            win32file.OPEN_EXISTING,
            0,
            None,
        )
    except Exception:
        return 3

    try:
        write_packet(handle, {"op": "ready", "pid": os.getpid()})
        while True:
            try:
                request = read_packet(handle)
            except (ValueError, UnicodeError) as error:
                try:
                    write_packet(handle, {"ok": False, "error": "管理员代理消息格式无效"})
                except Exception:
                    pass
                print(f"[AdminBroker] 消息格式无效: {error}", file=sys.stderr)
                return 4
            except (EOFError, OSError):
                return 0
            except Exception as error:
                print(f"[AdminBroker] 读取消息失败: {error}", file=sys.stderr)
                return 4
            operation = request.get("op")
            if operation == "shutdown":
                write_packet(handle, {"ok": True})
                return 0
            if operation != "execute":
                write_packet(handle, {"ok": False, "error": "未知代理操作"})
                continue
            write_packet(handle, _execute(request))
    except (EOFError, OSError):
        return 0
    finally:
        try:
            win32file.CloseHandle(handle)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].startswith(r"\\.\pipe\NotmyFaultAdmin-"):
        return 2
    return run(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
