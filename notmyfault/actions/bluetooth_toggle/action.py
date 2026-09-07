"""通过系统蓝牙无线电接口查询或切换蓝牙状态。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


_ACTIONS = frozenset({"toggle", "on", "off", "query"})
_WINDOWS_ERRORS = {
    "no_radio": "未发现蓝牙无线电",
    "access_denied": "Windows 拒绝蓝牙控制权限",
    "state_change_denied": "Windows 拒绝更改蓝牙状态",
    "state_not_applied": "Windows 接受了请求，但蓝牙状态没有改变",
    "disabled_radio": "蓝牙无线电已被硬件开关或系统策略禁用",
    "winrt_unavailable": "当前 Windows 无法使用蓝牙无线电接口",
}


def _decode_payload(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("蓝牙辅助程序没有返回有效结果")


def _windows_failure(
    result: subprocess.CompletedProcess[str],
    payload: dict[str, Any] | None,
) -> RuntimeError:
    if payload is not None:
        code = str(payload.get("code", ""))
        message = _WINDOWS_ERRORS.get(code) or str(payload.get("message", "")).strip()
        detail = str(payload.get("detail", "")).strip()
        if message and detail and detail != message:
            message = f"{message}：{detail}"
        if message:
            return RuntimeError(message)
    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        return RuntimeError(f"蓝牙辅助程序执行失败：{detail[-500:]}")
    return RuntimeError(f"蓝牙辅助程序退出码为 {result.returncode}")


def _run_windows(action: str) -> dict[str, Any]:
    helper = Path(__file__).with_name("radio.ps1")
    if not helper.is_file():
        raise RuntimeError("蓝牙插件缺少 radio.ps1")
    command = [
        os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        ),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper),
        "-Action",
        action,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as error:
        raise RuntimeError("找不到 Windows PowerShell 5.1") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("等待 Windows 更改蓝牙状态超时") from error

    try:
        payload = _decode_payload(result.stdout)
    except RuntimeError:
        payload = None
    if result.returncode != 0 or payload is None or payload.get("ok") is not True:
        raise _windows_failure(result, payload)

    payload.pop("ok", None)
    return payload


def _bluetoothctl() -> str:
    executable = shutil.which("bluetoothctl")
    if executable is None:
        raise RuntimeError("未安装 bluetoothctl")
    return executable


def _run_bluetoothctl(executable: str, *args: str, deadline=None, cancellation=None) -> subprocess.CompletedProcess[str]:
    if cancellation:
        cancellation.raise_if_cancelled()
    timeout = min(8, deadline - time.monotonic()) if deadline is not None else 8
    if timeout <= 0:
        raise RuntimeError("等待蓝牙状态超过总时限（30s）")
    try:
        return subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("等待 bluetoothctl 超时") from error
    except OSError as error:
        raise RuntimeError(f"无法执行 bluetoothctl：{error}") from error


def _linux_state(executable: str, deadline=None, cancellation=None) -> str:
    result = _run_bluetoothctl(executable, "show", deadline=deadline, cancellation=cancellation)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or "bluetoothctl show 执行失败")
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "powered":
            normalized = value.strip().lower()
            if normalized == "yes":
                return "on"
            if normalized == "no":
                return "off"
    raise RuntimeError("bluetoothctl 没有返回默认蓝牙控制器的电源状态")


def _run_linux(action: str, cancellation=None) -> dict[str, Any]:
    executable = _bluetoothctl()
    deadline = time.monotonic() + 30
    before = _linux_state(executable, deadline, cancellation)
    if action == "query":
        return {
            "action": action,
            "state": before,
            "changed": False,
            "method": "bluetoothctl",
        }

    target = ("off" if before == "on" else "on") if action == "toggle" else action
    if before == target:
        return {
            "action": action,
            "state": before,
            "changed": False,
            "method": "bluetoothctl",
        }

    result = _run_bluetoothctl(executable, "power", target, deadline=deadline, cancellation=cancellation)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"bluetoothctl power {target} 执行失败")

    actual = before
    for _attempt in range(10):
        if cancellation:
            if cancellation.wait(0.2):
                cancellation.raise_if_cancelled()
        else:
            time.sleep(0.2)
        actual = _linux_state(executable, deadline, cancellation)
        if actual == target:
            return {
                "action": action,
                "state": actual,
                "changed": True,
                "method": "bluetoothctl",
            }
    raise RuntimeError(f"请求蓝牙切换到 {target}，回读状态仍为 {actual}")


def run_with_context(_action_info: dict[str, Any], params: dict[str, Any], context) -> dict[str, Any]:
    action = str(params.get("action", "toggle")).strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"不支持的蓝牙操作: {action}")
    cancellation = context.get("runtime", {}).get("cancellation")
    if cancellation:
        cancellation.raise_if_cancelled()
    if sys.platform == "win32":
        result = _run_windows(action)
        if cancellation:
            cancellation.raise_if_cancelled()
        return result
    if sys.platform.startswith("linux"):
        return _run_linux(action, cancellation)
    raise RuntimeError("蓝牙开关插件仅支持 Windows 和 Linux")


def run(action_info, params):
    return run_with_context(action_info, params, {})
