from __future__ import annotations

from contextlib import contextmanager
from ctypes import POINTER, cast

from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform.errors import (
    BackendError,
    BackendFailedError,
    BackendInvalidError,
    BackendMissingError,
    BackendPermissionDeniedError,
)

_ROLE_LABEL = {0: "通用", 1: "多媒体", 2: "通信"}


@contextmanager
def _com():
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities
    except ImportError as error:
        raise BackendMissingError("音频控制需要 pycaw 和 comtypes") from error
    initialized = False
    try:
        comtypes.CoInitialize()
        initialized = True
        yield comtypes, AudioUtilities
    except BackendError:
        raise
    except Exception as error:
        hresult = getattr(error, "hresult", 0) & 0xffffffff
        if isinstance(error, PermissionError) or hresult == 0x80070005:
            raise BackendPermissionDeniedError("Windows 拒绝音频控制权限") from error
        raise BackendFailedError(f"Windows 音频操作失败: {error}") from error
    finally:
        if initialized:
            comtypes.CoUninitialize()


def _policy_type(comtypes):
    from ctypes import c_int, c_longlong, c_void_p
    from ctypes.wintypes import LPCWSTR

    COMMETHOD = comtypes.COMMETHOD
    HRESULT = comtypes.HRESULT

    # IPolicyConfig vtable 里 SetDefaultEndpoint 是第 11 个方法
    class IPolicyConfig(comtypes.IUnknown):
        _iid_ = comtypes.GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = (
            COMMETHOD([], HRESULT, "GetMixFormat",
                      (["in"], LPCWSTR, "device_id"),
                      (["out"], POINTER(c_void_p), "format")),
            COMMETHOD([], HRESULT, "GetDeviceFormat",
                      (["in"], LPCWSTR, "device_id"),
                      (["in"], c_int, "default"),
                      (["out"], POINTER(c_void_p), "format")),
            COMMETHOD([], HRESULT, "ResetDeviceFormat",
                      (["in"], LPCWSTR, "device_id")),
            COMMETHOD([], HRESULT, "SetDeviceFormat",
                      (["in"], LPCWSTR, "device_id"),
                      (["in"], c_void_p, "endpoint_format"),
                      (["in"], c_void_p, "mix_format")),
            COMMETHOD([], HRESULT, "GetProcessingPeriod",
                      (["in"], LPCWSTR, "device_id"),
                      (["in"], c_int, "default"),
                      (["out"], POINTER(c_longlong), "default_period"),
                      (["out"], POINTER(c_longlong), "minimum_period")),
            COMMETHOD([], HRESULT, "SetProcessingPeriod",
                      (["in"], LPCWSTR, "device_id"),
                      (["in"], POINTER(c_longlong), "period")),
            COMMETHOD([], HRESULT, "GetShareMode",
                      (["in"], LPCWSTR, "device_id"),
                      (["out"], POINTER(c_void_p), "mode")),
            COMMETHOD([], HRESULT, "SetShareMode",
                      (["in"], LPCWSTR, "device_id"),
                      (["in"], c_void_p, "mode")),
            COMMETHOD([], HRESULT, "GetPropertyValue",
                      (["in"], LPCWSTR, "device_id"),
                      (["in"], c_void_p, "key"),
                      (["out"], POINTER(c_void_p), "value")),
            COMMETHOD([], HRESULT, "SetPropertyValue",
                      (["in"], LPCWSTR, "device_id"),
                      (["in"], c_void_p, "key"),
                      (["in"], c_void_p, "value")),
            COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                      (["in"], LPCWSTR, "device_id"),
                      (["in"], c_int, "role")),
            COMMETHOD([], HRESULT, "SetEndpointVisibility",
                      (["in"], LPCWSTR, "device_id"),
                      (["in"], c_int, "visible")),
        )

    return IPolicyConfig


class AudioBackend:
    def __init__(self, runner=None) -> None:
        self._runner = runner

    @staticmethod
    def _endpoint(comtypes, utilities):
        device = utilities.GetSpeakers()
        if device is None:
            raise BackendFailedError("未找到音频输出设备")
        endpoint = getattr(device, "EndpointVolume", None)
        if endpoint is not None:
            return endpoint
        if hasattr(device, "SetMute"):
            return device
        from pycaw.pycaw import IAudioEndpointVolume
        return cast(device.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None),
                    POINTER(IAudioEndpointVolume))

    def set_volume(self, percent: int) -> None:
        if type(percent) is not int or not 0 <= percent <= 100:
            raise BackendInvalidError("音量必须是 0 到 100 的整数")
        with NATIVE_LOCK, _com() as (comtypes, utilities):
            endpoint = self._endpoint(comtypes, utilities)
            scalar = percent / 100
            endpoint.SetMasterVolumeLevelScalar(scalar, None)
            actual = endpoint.GetMasterVolumeLevelScalar()
            if abs(actual - scalar) > 0.01:
                raise BackendFailedError(f"音量设置未生效（期望 {scalar:.2f}，实际 {actual:.2f}）")

    def set_muted(self, muted: bool) -> None:
        if type(muted) is not bool:
            raise BackendInvalidError("静音状态必须是布尔值")
        with NATIVE_LOCK, _com() as (comtypes, utilities):
            endpoint = self._endpoint(comtypes, utilities)
            endpoint.SetMute(muted, None)
            if bool(endpoint.GetMute()) != muted:
                raise BackendFailedError("Windows 拒绝设置静音状态")

    @staticmethod
    def _devices(utilities) -> list[dict[str, str]]:
        enumerator = utilities.GetDeviceEnumerator()
        devices = []
        for flow_id, flow in ((0, "render"), (1, "capture")):
            collection = enumerator.EnumAudioEndpoints(flow_id, 0x1)
            for index in range(collection.GetCount()):
                device_id = str(collection.Item(index).GetId())
                devices.append({"id": device_id, "name": device_id, "flow": flow})
        names = {}
        for device in utilities.GetAllDevices():
            device_id = getattr(device, "id", None)
            friendly = getattr(device, "FriendlyName", None)
            if device_id and friendly:
                names[str(device_id)] = str(friendly)
        for device in devices:
            device["name"] = names.get(device["id"], device["id"])
        return devices

    @staticmethod
    def _default(comtypes, enumerator, flow: str, role: int = 0) -> str:
        try:
            return str(enumerator.GetDefaultAudioEndpoint(0 if flow == "render" else 1, role).GetId())
        except comtypes.COMError as error:
            if getattr(error, "hresult", 0) & 0xffffffff == 0x80070490:
                return ""
            if getattr(error, "hresult", 0) & 0xffffffff == 0x80070005:
                raise BackendPermissionDeniedError("Windows 拒绝读取默认音频设备") from error
            raise BackendFailedError("读取默认音频设备失败") from error

    @staticmethod
    def _set_default(comtypes, device_id: str) -> None:
        policy = comtypes.CoCreateInstance(
            comtypes.GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"),
            _policy_type(comtypes), comtypes.CLSCTX_ALL,
        )
        for role in _ROLE_LABEL:
            hr = policy.SetDefaultEndpoint(device_id, role)
            if isinstance(hr, int) and hr < 0:
                if hr & 0xffffffff == 0x80070005:
                    raise BackendPermissionDeniedError("Windows 拒绝设置默认音频设备")
                raise BackendFailedError(
                    f"Windows 拒绝设置默认音频设备（HRESULT=0x{hr & 0xffffffff:08X}）"
                )

    def devices(self) -> list[dict[str, str]]:
        with NATIVE_LOCK, _com() as (_, utilities):
            return self._devices(utilities)

    def default_devices(self) -> dict[str, str]:
        with NATIVE_LOCK, _com() as (comtypes, utilities):
            enumerator = utilities.GetDeviceEnumerator()
            return {flow: self._default(comtypes, enumerator, flow) for flow in ("render", "capture")}

    def set_default(self, flow: str, device_id: str) -> dict:
        flow = str(flow or "").strip().lower()
        if flow not in ("render", "capture"):
            raise BackendInvalidError(f"无效的音频流向: {flow!r}（可选: render/capture）")
        target = str(device_id or "").strip()
        if not target:
            raise BackendInvalidError("必须填写目标音频设备 id 或名称")
        with NATIVE_LOCK, _com() as (comtypes, utilities):
            enumerator = utilities.GetDeviceEnumerator()
            defaults = {role: self._default(comtypes, enumerator, flow, role) for role in _ROLE_LABEL}
            candidates = [device for device in self._devices(utilities) if device["flow"] == flow]
            matches = [device for device in candidates if device["id"] == target]
            if not matches:
                matches = [device for device in candidates if target.casefold() in device["name"].casefold()]
            if len(matches) != 1:
                raise BackendInvalidError(f"目标设备 {target!r} 匹配到 {len(matches)} 个{flow}设备，需要唯一设备 id 或名称")
            selected = matches[0]
            changed = any(current != selected["id"] for current in defaults.values())
            if changed:
                self._set_default(comtypes, selected["id"])
                for role, label in _ROLE_LABEL.items():
                    actual = self._default(comtypes, enumerator, flow, role)
                    if actual != selected["id"]:
                        raise BackendFailedError(f"请求默认设备 {selected['name']!r}，{label}角色回读为 {actual!r}")
            return {
                "device_id": selected["id"],
                "device_name": selected["name"],
                "previous_device_id": defaults[0],
                "changed": changed,
                "method": "PolicyConfig",
            }
