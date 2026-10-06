"""设置默认播放或录音设备：Windows 用 PolicyConfig，Linux 用 wpctl/pactl"""

from __future__ import annotations

import sys
from typing import Any


_FLOWS = frozenset({"render", "capture"})
_FLOW_LABEL = {"render": "播放", "capture": "录音"}
_ROLE_LABEL = {0: "通用", 1: "多媒体", 2: "通信"}


def _windows_endpoints(device_type: str) -> list[tuple[str, str]]:
    """返回 [(device_id, friendly_name), ...]，仅活动设备。"""
    from comtypes import CoInitialize, CoUninitialize
    from pycaw.pycaw import AudioUtilities

    flow = 0 if device_type == "render" else 1
    CoInitialize()
    try:
        enumerator = AudioUtilities.GetDeviceEnumerator()
        collection = enumerator.EnumAudioEndpoints(flow, 0x1)
        ids = [collection.Item(index).GetId() for index in range(collection.GetCount())]
        names = {device_id: device_id for device_id in ids}
        try:
            for device in AudioUtilities.GetAllDevices():
                device_id = getattr(device, "id", None)
                friendly = getattr(device, "FriendlyName", None)
                if device_id in names and friendly:
                    names[device_id] = str(friendly)
        except Exception:
            pass
        return [(device_id, names[device_id]) for device_id in ids]
    finally:
        CoUninitialize()


def _resolve_windows_device(device_type: str, target: str) -> tuple[str, str]:
    """按 id 或友好名解析设备，返回 (device_id, friendly_name)。"""
    devices = _windows_endpoints(device_type)
    exact_id = None
    name_matches: list[tuple[str, str]] = []
    for device_id, name in devices:
        if device_id == target:
            exact_id = (device_id, name)
            break
        if name.lower() == target.lower() or target.lower() in name.lower():
            name_matches.append((device_id, name))
    if exact_id is not None:
        return exact_id
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        names = "、".join(item[1] for item in name_matches)
        raise ValueError(f"设备名称 {target!r} 匹配到多个设备：{names}")
    hint = "；".join(f"{name} ({device_id})" for device_id, name in devices[:8]) or "无可用设备"
    raise ValueError(
        f"未找到{_FLOW_LABEL[device_type]}设备 {target!r}。当前可用：{hint}"
    )


def _set_windows_default(device_id: str) -> None:
    """通过未公开的 PolicyConfig COM 接口设置默认端点（三个角色都设）。"""
    from comtypes import (
        CLSCTX_ALL,
        CoCreateInstance,
        CoInitialize,
        CoUninitialize,
        COMMETHOD,
        GUID,
        HRESULT,
        IUnknown,
    )
    from ctypes import POINTER, c_int, c_longlong, c_void_p
    from ctypes.wintypes import LPCWSTR

    from notmyfault.plugin_api import native_lock

    # IPolicyConfig vtable 里 SetDefaultEndpoint 是第 11 个方法
    class IPolicyConfig(IUnknown):
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = (
            COMMETHOD(
                [], HRESULT, "GetMixFormat",
                (["in"], LPCWSTR, "device_id"),
                (["out"], POINTER(c_void_p), "format"),
            ),
            COMMETHOD(
                [], HRESULT, "GetDeviceFormat",
                (["in"], LPCWSTR, "device_id"),
                (["in"], c_int, "default"),
                (["out"], POINTER(c_void_p), "format"),
            ),
            COMMETHOD(
                [], HRESULT, "ResetDeviceFormat",
                (["in"], LPCWSTR, "device_id"),
            ),
            COMMETHOD(
                [], HRESULT, "SetDeviceFormat",
                (["in"], LPCWSTR, "device_id"),
                (["in"], c_void_p, "endpoint_format"),
                (["in"], c_void_p, "mix_format"),
            ),
            COMMETHOD(
                [], HRESULT, "GetProcessingPeriod",
                (["in"], LPCWSTR, "device_id"),
                (["in"], c_int, "default"),
                (["out"], POINTER(c_longlong), "default_period"),
                (["out"], POINTER(c_longlong), "minimum_period"),
            ),
            COMMETHOD(
                [], HRESULT, "SetProcessingPeriod",
                (["in"], LPCWSTR, "device_id"),
                (["in"], POINTER(c_longlong), "period"),
            ),
            COMMETHOD(
                [], HRESULT, "GetShareMode",
                (["in"], LPCWSTR, "device_id"),
                (["out"], POINTER(c_void_p), "mode"),
            ),
            COMMETHOD(
                [], HRESULT, "SetShareMode",
                (["in"], LPCWSTR, "device_id"),
                (["in"], c_void_p, "mode"),
            ),
            COMMETHOD(
                [], HRESULT, "GetPropertyValue",
                (["in"], LPCWSTR, "device_id"),
                (["in"], c_void_p, "key"),
                (["out"], POINTER(c_void_p), "value"),
            ),
            COMMETHOD(
                [], HRESULT, "SetPropertyValue",
                (["in"], LPCWSTR, "device_id"),
                (["in"], c_void_p, "key"),
                (["in"], c_void_p, "value"),
            ),
            COMMETHOD(
                [], HRESULT, "SetDefaultEndpoint",
                (["in"], LPCWSTR, "device_id"),
                (["in"], c_int, "role"),
            ),
            COMMETHOD(
                [], HRESULT, "SetEndpointVisibility",
                (["in"], LPCWSTR, "device_id"),
                (["in"], c_int, "visible"),
            ),
        )

    clsid = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
    CoInitialize()
    try:
        with native_lock():
            policy = CoCreateInstance(clsid, IPolicyConfig, CLSCTX_ALL)
            for role in (0, 1, 2):
                hr = policy.SetDefaultEndpoint(device_id, role)
                if isinstance(hr, int) and hr < 0:
                    raise RuntimeError(
                        f"Windows 拒绝设置默认音频设备（HRESULT=0x{hr & 0xffffffff:08X}）"
                    )
    finally:
        CoUninitialize()


def _query_default_windows(device_type: str, role: int = 0) -> str:
    from comtypes import CoInitialize, CoUninitialize, COMError
    from pycaw.pycaw import AudioUtilities

    flow = 0 if device_type == "render" else 1
    CoInitialize()
    try:
        enumerator = AudioUtilities.GetDeviceEnumerator()
        try:
            return enumerator.GetDefaultAudioEndpoint(flow, role).GetId()
        except COMError as error:
            if error.hresult & 0xffffffff == 0x80070490:
                return ""
            raise RuntimeError("读取默认音频设备失败") from error
    finally:
        CoUninitialize()


def _run_windows(device_type: str, target: str) -> dict[str, Any]:
    defaults = {role: _query_default_windows(device_type, role) for role in _ROLE_LABEL}
    previous = defaults[0]
    device_id, device_name = _resolve_windows_device(device_type, target)
    if all(current == device_id for current in defaults.values()):
        return {
            "device_type": device_type,
            "device_id": device_id,
            "device_name": device_name,
            "previous_device_id": previous,
            "changed": False,
            "method": "PolicyConfig",
        }
    _set_windows_default(device_id)
    actual = {role: _query_default_windows(device_type, role) for role in _ROLE_LABEL}
    for role, current in actual.items():
        if current != device_id:
            raise RuntimeError(
                f"请求将默认{_FLOW_LABEL[device_type]}设备设为 {device_name!r}，"
                f"{_ROLE_LABEL[role]}角色回读为 {current!r}"
            )
    return {
        "device_type": device_type,
        "device_id": device_id,
        "device_name": device_name,
        "previous_device_id": previous,
        "changed": True,
        "method": "PolicyConfig",
    }


def _run_linux(device_type: str, target: str) -> dict[str, Any]:
    from notmyfault.plugin_api import PlatformServiceError, platform_services

    services = platform_services()
    try:
        current = services.default_audio_devices()
    except PlatformServiceError as error:
        raise RuntimeError(str(error)) from error
    previous = current.get(device_type, "")
    if previous == target:
        return {
            "device_type": device_type,
            "device_id": previous,
            "previous_device_id": previous,
            "changed": False,
            "method": services.capability("audio.device_query").backend or "wpctl/pactl",
        }
    try:
        services.set_default_audio_device(device_type, target)
        after = services.default_audio_devices()
    except PlatformServiceError as error:
        raise RuntimeError(str(error)) from error
    actual = after.get(device_type, "")
    if actual != target:
        raise RuntimeError(
            f"请求将默认{_FLOW_LABEL[device_type]}设备设为 {target!r}，回读为 {actual!r}"
        )
    backend = services.capability("audio.device_query").backend or "wpctl/pactl"
    return {
        "device_type": device_type,
        "device_id": actual,
        "previous_device_id": previous,
        "changed": True,
        "method": backend,
    }


def run(action_info, params):
    device_type = str(params.get("device_type", "render") or "render").strip().lower()
    if device_type not in _FLOWS:
        raise ValueError(
            f"无效的设备类型: {device_type!r}（可选: render/capture）"
        )
    target = str(params.get("device", "") or "").strip()
    if not target:
        raise ValueError("必须填写目标设备 id 或名称")

    print(f"[Action:set_default_audio] 设置默认{_FLOW_LABEL[device_type]}设备: {target}")
    if sys.platform == "win32":
        result = _run_windows(device_type, target)
    elif sys.platform.startswith("linux"):
        result = _run_linux(device_type, target)
    else:
        raise RuntimeError("设置默认音频设备仅支持 Windows 和 Linux")
    print(
        f"[Action:set_default_audio] 完成: {result['device_id']!r} "
        f"changed={result['changed']}"
    )
    return result
