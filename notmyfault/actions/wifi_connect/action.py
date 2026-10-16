"""连接或断开 WiFi：Windows 用 netsh wlan，Linux 用 nmcli"""

from __future__ import annotations

from notmyfault.plugin_api import action_values

import sys
import os
import re
import shutil
import subprocess
import time
from typing import Any


_ACTIONS = frozenset({"connect", "disconnect"})
_password_warning_shown = False


def _warn_password_in_argv() -> None:
    """连未保存过的新网络时，密码只能放进 nmcli 命令行

    NetworkManager 的 passwd-file 只适用于 connection up；这条路径要先创建并激活
    连接配置，改成不经过命令行传密码需要在有 nmcli 的 Linux 上验证。
    """
    global _password_warning_shown
    if _password_warning_shown:
        return
    _password_warning_shown = True
    print(
        "[Action:wifi_connect] 提醒：nmcli 的密码会出现在进程命令行中，"
        "同机其他用户可能通过 /proc 看到（取决于 /proc 挂载与访问权限）；"
        "先手动连接一次该网络，之后密码留空即可",
        file=sys.stderr,
    )


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


def _current_ssid_windows(interface_guid: str | None = None) -> str | None:
    from notmyfault.platform.windows_wifi import current_ssid

    return current_ssid(interface_guid)


def _current_ssid_linux(device: str | None = None) -> str | None:
    if not shutil.which("nmcli"):
        return None
    command = ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"]
    if device is not None:
        command.extend(["ifname", device])
    try:
        result = _run_command(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            env={**os.environ, "LC_ALL": "C"},
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


def _wait_for_ssid(predicate, *, timeout: float = 20.0, query=None) -> str | None:
    """轮询直到当前 SSID 满足条件或超时，返回最后读到的 SSID。"""
    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        last = (query or _current_ssid)()
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


def _saved_profile_windows(ssid: str) -> tuple[str, str, str] | None:
    import ctypes
    import xml.etree.ElementTree as ET
    import uuid

    from notmyfault.plugin_api import native_lock

    DWORD = ctypes.c_uint32
    PVOID = ctypes.c_void_p

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", DWORD), ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8),
        ]

    class InterfaceInfo(ctypes.Structure):
        _fields_ = [
            ("guid", GUID), ("description", ctypes.c_wchar * 256), ("state", DWORD),
        ]

    class ProfileInfo(ctypes.Structure):
        _fields_ = [("name", ctypes.c_wchar * 256), ("flags", DWORD)]

    def items(pointer, item_type):
        count = ctypes.cast(pointer, ctypes.POINTER(DWORD))[0]
        return ctypes.cast(pointer.value + 8, ctypes.POINTER(item_type * count)).contents

    def check(code, operation):
        if code:
            raise RuntimeError(f"Windows 无线配置查询失败：{operation}（错误码 {code}）")

    target = ssid.encode("utf-8")
    with native_lock():
        try:
            wlan = ctypes.WinDLL("wlanapi")
            iphlp = ctypes.WinDLL("iphlpapi")
        except OSError as error:
            raise RuntimeError(f"加载 Windows 无线接口失败：{error}") from error
        signatures = [
            (wlan, "WlanOpenHandle", [DWORD, PVOID, ctypes.POINTER(DWORD), ctypes.POINTER(PVOID)]),
            (wlan, "WlanCloseHandle", [PVOID, PVOID]),
            (wlan, "WlanEnumInterfaces", [PVOID, PVOID, ctypes.POINTER(PVOID)]),
            (wlan, "WlanGetProfileList", [PVOID, ctypes.POINTER(GUID), PVOID, ctypes.POINTER(PVOID)]),
            (wlan, "WlanGetProfile", [
                PVOID, ctypes.POINTER(GUID), ctypes.c_wchar_p, PVOID,
                ctypes.POINTER(PVOID), ctypes.POINTER(DWORD), ctypes.POINTER(DWORD),
            ]),
            (iphlp, "ConvertInterfaceGuidToLuid", [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_uint64)]),
            (iphlp, "ConvertInterfaceLuidToAlias", [
                ctypes.POINTER(ctypes.c_uint64), ctypes.c_wchar_p, ctypes.c_size_t,
            ]),
        ]
        for library, name, arguments in signatures:
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = DWORD
        wlan.WlanFreeMemory.argtypes = [PVOID]
        wlan.WlanFreeMemory.restype = None

        handle = PVOID()
        version = DWORD()
        check(wlan.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle)), "WlanOpenHandle")
        interfaces = PVOID()
        try:
            check(wlan.WlanEnumInterfaces(handle, None, ctypes.byref(interfaces)), "WlanEnumInterfaces")
            for interface in items(interfaces, InterfaceInfo):
                profiles = PVOID()
                try:
                    check(wlan.WlanGetProfileList(
                        handle, ctypes.byref(interface.guid), None, ctypes.byref(profiles),
                    ), "WlanGetProfileList")
                    for profile in items(profiles, ProfileInfo):
                        xml = PVOID()
                        flags = DWORD(0)
                        access = DWORD()
                        try:
                            check(wlan.WlanGetProfile(
                                handle, ctypes.byref(interface.guid), profile.name, None,
                                ctypes.byref(xml), ctypes.byref(flags), ctypes.byref(access),
                            ), "WlanGetProfile")
                            root = ET.fromstring(ctypes.wstring_at(xml))
                            matched = False
                            for network in root.findall("./{*}SSIDConfig/{*}SSID"):
                                hex_ssid = network.findtext("{*}hex")
                                if hex_ssid is not None:
                                    matched = bytes.fromhex(hex_ssid) == target
                                else:
                                    matched = network.findtext("{*}name") == ssid
                                if matched:
                                    break
                        except (ET.ParseError, ValueError) as error:
                            raise RuntimeError("Windows 无线配置中的 SSID 数据无效") from error
                        finally:
                            if xml.value:
                                wlan.WlanFreeMemory(xml)
                        if matched:
                            luid = ctypes.c_uint64()
                            alias = ctypes.create_unicode_buffer(257)
                            check(iphlp.ConvertInterfaceGuidToLuid(
                                ctypes.byref(interface.guid), ctypes.byref(luid),
                            ), "ConvertInterfaceGuidToLuid")
                            check(iphlp.ConvertInterfaceLuidToAlias(
                                ctypes.byref(luid), alias, len(alias),
                            ), "ConvertInterfaceLuidToAlias")
                            interface_guid = str(uuid.UUID(bytes_le=bytes(interface.guid)))
                            return profile.name, alias.value, interface_guid
                finally:
                    if profiles.value:
                        wlan.WlanFreeMemory(profiles)
        finally:
            if interfaces.value:
                wlan.WlanFreeMemory(interfaces)
            wlan.WlanCloseHandle(handle, None)
    return None


def _run_windows(action: str, ssid: str, _password: str) -> dict[str, Any]:
    previous = _current_ssid_windows()
    if previous is None:
        raise RuntimeError("Wi-Fi 查询失败，请检查无线服务及 WLAN API")

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

    saved = _saved_profile_windows(ssid)
    if saved is None:
        raise RuntimeError(f"未找到 SSID {ssid!r} 的已保存 Wi-Fi 配置")
    profile_name, interface_name, interface_guid = saved
    previous = _current_ssid_windows(interface_guid)
    if previous is None:
        raise RuntimeError("无法查询所选无线网卡的 Wi-Fi 状态")
    if previous == ssid:
        return {
            "action": action,
            "ssid": previous,
            "previous_ssid": previous,
            "connected": True,
            "changed": False,
            "method": "netsh",
        }
    try:
        result = _run_command(
            ["netsh", "wlan", "connect", f"name={profile_name}", f"ssid={ssid}",
             f"interface={interface_name}"],
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
    actual = _wait_for_ssid(
        lambda value: value == ssid,
        query=lambda: _current_ssid_windows(interface_guid),
    )
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
            env={**os.environ, "LC_ALL": "C"},
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


def _saved_connection_uuid(executable: str, ssid: str) -> str | None:
    result = _run_nmcli(executable, "-t", "-f", "UUID,TYPE", "connection", "show", timeout=10)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or "nmcli 连接配置列表查询失败")
    for line in result.stdout.splitlines():
        connection_uuid, _, conn_type = line.rpartition(":")
        if not connection_uuid or conn_type not in ("802-11-wireless", "wifi"):
            continue
        profile = _run_nmcli(
            executable, "-g", "802-11-wireless.ssid", "-e", "no",
            "connection", "show", "uuid", connection_uuid, timeout=10,
        )
        if profile.returncode != 0:
            detail = (profile.stderr or profile.stdout or "").strip()
            raise RuntimeError(detail or "nmcli 无线连接配置查询失败")
        if profile.stdout.rstrip("\r\n") == ssid:
            return connection_uuid
    return None


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
        actual = _wait_for_ssid(
            lambda value: value == "",
            query=lambda: _current_ssid_linux(device),
        )
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

    connection_uuid = _saved_connection_uuid(executable, ssid)
    if connection_uuid is not None:
        result = _run_nmcli(executable, "connection", "up", "uuid", connection_uuid)
    elif password:
        _warn_password_in_argv()
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


def run_with_context(action_info, params, context):
    params = action_values(params)
    action = str(params.get("action", "connect") or "connect").strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"不支持的 WiFi 操作: {action}")
    ssid = str(params.get("ssid", "") or "")
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
