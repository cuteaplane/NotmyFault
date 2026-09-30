"""连接或断开 WiFi：Windows 用 netsh wlan，Linux 用 nmcli"""

from __future__ import annotations

import sys
import re
import shutil
import subprocess
import time
from typing import Any


_ACTIONS = frozenset({"connect", "disconnect"})
_SSID_LINE_RE = re.compile(r"^\s*SSID\s*:\s*(.*)$", re.IGNORECASE)


def _run_command(command: list[str], *, timeout: float, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, timeout=timeout, check=False, **kwargs)


def _decode_netsh(raw: bytes) -> str:
    """netsh 使用当前控制台输出代码页，无控制台时使用系统 OEM 代码页。"""
    import ctypes

    from notmyfault.plugin_api import native_lock

    with native_lock():
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetConsoleOutputCP.restype = ctypes.c_uint
        kernel32.GetOEMCP.restype = ctypes.c_uint
        codepage = kernel32.GetConsoleOutputCP() or kernel32.GetOEMCP()
    return raw.decode(f"cp{codepage}", errors="replace")


def _current_ssid_windows() -> str | None:
    try:
        result = _run_command(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = _decode_netsh(result.stdout)
    for line in text.splitlines():
        match = _SSID_LINE_RE.match(line)
        if match:
            return match.group(1).strip().strip('"')
    return ""


def _current_ssid_linux() -> str | None:
    if not shutil.which("nmcli"):
        return None
    try:
        result = _run_command(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("yes:"):
            return re.sub(r"\\([\\:])", r"\1", line[4:])
    return ""


def _current_ssid() -> str | None:
    if sys.platform == "win32":
        return _current_ssid_windows()
    return _current_ssid_linux()


def _wait_for_ssid(predicate, *, timeout: float = 20.0) -> str | None:
    """轮询直到当前 SSID 满足条件或超时，返回最后读到的 SSID。"""
    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        last = _current_ssid()
        if last is not None and predicate(last):
            return last
        time.sleep(0.4)
    return last


def _windows_failure(result: subprocess.CompletedProcess[bytes], fallback: str) -> RuntimeError:
    detail = ""
    if result.stdout:
        detail = _decode_netsh(result.stdout).strip()
    if not detail and result.stderr:
        detail = _decode_netsh(result.stderr).strip()
    if detail:
        return RuntimeError(f"{fallback}：{detail[-400:]}")
    return RuntimeError(f"{fallback}（退出码 {result.returncode}）")


def _run_windows(action: str, ssid: str, _password: str) -> dict[str, Any]:
    previous = _current_ssid_windows()
    if previous is None:
        raise RuntimeError("Wi-Fi 查询失败，请检查无线服务及 netsh")

    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if action == "disconnect":
        if not previous:
            return {
                "action": action,
                "ssid": "",
                "previous_ssid": "",
                "connected": False,
                "changed": False,
                "method": "netsh",
            }
        try:
            result = _run_command(
                ["netsh", "wlan", "disconnect"],
                capture_output=True,
                timeout=30,
                creationflags=creation,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"执行 netsh 失败：{error}") from error
        if result.returncode != 0:
            raise _windows_failure(result, "断开 WiFi 失败")
        actual = _wait_for_ssid(lambda value: value == "")
        if actual is None or actual != "":
            raise RuntimeError(
                f"已请求断开 WiFi，回读仍为 {actual!r}"
            )
        return {
            "action": action,
            "ssid": "",
            "previous_ssid": previous,
            "connected": False,
            "changed": True,
            "method": "netsh",
        }

    if previous == ssid:
        return {
            "action": action,
            "ssid": previous,
            "previous_ssid": previous,
            "connected": True,
            "changed": False,
            "method": "netsh",
        }

    # Windows 只能连接已保存的配置文件；配置名通常与 SSID 相同
    try:
        result = _run_command(
            ["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}"],
            capture_output=True,
            timeout=30,
            creationflags=creation,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"执行 netsh 失败：{error}") from error
    if result.returncode != 0:
        raise _windows_failure(
            result,
            "连接 WiFi 失败（请确认该网络已在系统中保存过）",
        )
    actual = _wait_for_ssid(lambda value: value == ssid)
    if actual is None:
        raise RuntimeError("连接后无法查询 WiFi 状态")
    if actual != ssid:
        raise RuntimeError(f"请求连接 {ssid!r}，回读为 {actual!r}")
    return {
        "action": action,
        "ssid": actual,
        "previous_ssid": previous,
        "connected": True,
        "changed": previous != actual,
        "method": "netsh",
    }


def _nmcli() -> str:
    executable = shutil.which("nmcli")
    if executable is None:
        raise RuntimeError("未安装 nmcli")
    return executable


def _run_nmcli(executable: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    try:
        return _run_command(
            [executable, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("等待 nmcli 超时") from error
    except OSError as error:
        raise RuntimeError(f"无法执行 nmcli：{error}") from error


def _wifi_device(executable: str) -> str:
    result = _run_nmcli(executable, "-t", "-f", "DEVICE,TYPE,STATE", "device", "status", timeout=10)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or "nmcli device status 执行失败")
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "wifi":
            return parts[0]
    raise RuntimeError("未发现 WiFi 网卡")


def _has_connection(executable: str, ssid: str) -> bool:
    result = _run_nmcli(executable, "-t", "-f", "NAME,TYPE", "connection", "show", timeout=10)
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        name, _, rest = line.partition(":")
        conn_type = rest.split(":", 1)[0]
        if name == ssid and conn_type in ("802-11-wireless", "wifi"):
            return True
    return False


def _run_linux(action: str, ssid: str, password: str) -> dict[str, Any]:
    executable = _nmcli()
    previous = _current_ssid_linux()
    if previous is None:
        raise RuntimeError("Wi-Fi 查询失败，请检查无线服务及 nmcli")

    if action == "disconnect":
        if not previous:
            return {
                "action": action,
                "ssid": "",
                "previous_ssid": "",
                "connected": False,
                "changed": False,
                "method": "nmcli",
            }
        device = _wifi_device(executable)
        result = _run_nmcli(executable, "device", "disconnect", device)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(detail or "断开 WiFi 失败")
        actual = _wait_for_ssid(lambda value: value == "")
        if actual is None or actual != "":
            raise RuntimeError(f"已请求断开 WiFi，回读仍为 {actual!r}")
        return {
            "action": action,
            "ssid": "",
            "previous_ssid": previous,
            "connected": False,
            "changed": True,
            "method": "nmcli",
        }

    if previous == ssid:
        return {
            "action": action,
            "ssid": previous,
            "previous_ssid": previous,
            "connected": True,
            "changed": False,
            "method": "nmcli",
        }

    if _has_connection(executable, ssid):
        result = _run_nmcli(executable, "connection", "up", "id", ssid)
    elif password:
        result = _run_nmcli(
            executable, "device", "wifi", "connect", ssid, "password", password
        )
    else:
        result = _run_nmcli(executable, "device", "wifi", "connect", ssid)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"连接 WiFi {ssid!r} 失败")

    actual = _wait_for_ssid(lambda value: value == ssid)
    if actual is None:
        raise RuntimeError("连接后无法查询 WiFi 状态")
    if actual != ssid:
        raise RuntimeError(f"请求连接 {ssid!r}，回读为 {actual!r}")
    return {
        "action": action,
        "ssid": actual,
        "previous_ssid": previous,
        "connected": True,
        "changed": previous != actual,
        "method": "nmcli",
    }


def run(action_info, params):
    action = str(params.get("action", "connect") or "connect").strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"不支持的 WiFi 操作: {action}")
    ssid = str(params.get("ssid", "") or "").strip()
    password = str(params.get("password", "") or "")
    if action == "connect" and not ssid:
        raise ValueError("连接 WiFi 时必须填写目标 SSID")

    print(f"[Action:wifi_connect] 执行: {action}" + (f" -> {ssid}" if ssid else ""))
    if sys.platform == "win32":
        result = _run_windows(action, ssid, password)
    elif sys.platform.startswith("linux"):
        result = _run_linux(action, ssid, password)
    else:
        raise RuntimeError("连接 WiFi 仅支持 Windows 和 Linux")
    print(
        f"[Action:wifi_connect] 完成: connected={result['connected']} "
        f"ssid={result['ssid']!r} changed={result['changed']}"
    )
    return result
