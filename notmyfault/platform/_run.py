from __future__ import annotations

import os
import shutil
import subprocess
import threading

from notmyfault.platform.errors import (
    BackendFailedError,
    BackendMissingError,
    BackendPermissionDeniedError,
)


def command_path(*names: str) -> str | None:
    for name in names:
        if path := shutil.which(name):
            return path
    return None


def require_command(purpose: str, *names: str) -> str:
    path = command_path(*names)
    if path:
        return path
    raise BackendMissingError(f"依赖缺失：{purpose} 需要 " + " 或 ".join(names))


def powershell_path() -> str:
    return os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )


def failed_detail(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or "").strip() or f"退出码 {result.returncode}"


class CommandRunner:
    def read_output(self, args: list[str], max_chars: int, timeout: float) -> subprocess.CompletedProcess:
        timed_out = threading.Event()
        try:
            with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, errors="replace") as process:
                def expire():
                    timed_out.set()
                    if process.poll() is None:
                        process.kill()

                timer = threading.Timer(timeout, expire)
                timer.daemon = True
                timer.start()
                try:
                    output = process.stdout.read(max_chars)
                    truncated = len(output) == max_chars
                    if truncated and process.poll() is None:
                        process.kill()
                    process.wait()
                finally:
                    timer.cancel()
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                if timed_out.is_set():
                    raise BackendFailedError(f"命令超时（{timeout}s）: {args[0]}")
                return subprocess.CompletedProcess(args, 0 if truncated else process.returncode, output, "")
        except PermissionError as exc:
            raise BackendPermissionDeniedError(f"系统拒绝执行: {args[0]}") from exc
        except FileNotFoundError as exc:
            raise BackendMissingError(f"未安装 {args[0]}") from exc

    def run(
        self,
        args: list[str],
        timeout: float = 10,
        input: str | None = None,
        env: dict[str, str] | None = None,
        encoding: str | None = None,
        creationflags: int = 0,
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding=encoding,
                errors="replace",
                timeout=timeout,
                input=input,
                env=env,
                check=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendFailedError(
                f"命令超时（{timeout}s）: {args[0] if args else '?'}"
            ) from exc
        except PermissionError as exc:
            raise BackendPermissionDeniedError(f"系统拒绝执行: {args[0]}") from exc
        except FileNotFoundError as exc:
            raise BackendMissingError(f"未安装 {args[0]}") from exc

    def which(self, *names: str) -> str | None:
        return command_path(*names)


default_runner = CommandRunner()
