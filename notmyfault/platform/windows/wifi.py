from __future__ import annotations

import ctypes
import os
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform._run import default_runner
from notmyfault.platform.errors import (
    BackendError, BackendFailedError, BackendInvalidError, BackendMissingError,
    BackendPermissionDeniedError,
)
from notmyfault.platform.windows import _win32 as w


def _check(code, operation):
    if code:
        error = BackendPermissionDeniedError if code == 5 else BackendFailedError
        raise error(f"Windows 无线查询失败：{operation}（错误码 {code}）")


def _items(pointer, record_type):
    if not pointer.value:
        raise BackendFailedError("Windows 无线查询未返回数据")
    count = ctypes.cast(pointer, ctypes.POINTER(w.WLAN_DWORD))[0]
    return ctypes.cast(pointer.value + 8, ctypes.POINTER(record_type * count)).contents


def _profile_name(profile):
    return bytes(profile.name).decode("utf-16-le").split("\0", 1)[0]


@contextmanager
def _private_profile(content):
    try:
        import win32api
        import win32con
        import win32file
        import win32security
    except ImportError:
        raise BackendMissingError("WiFi 密码连接需要 pywin32") from None
    path = Path(tempfile.gettempdir()) / f"notmyfault-wifi-{uuid.uuid4().hex}.xml"
    created = False
    try:
        with NATIVE_LOCK:
            volume = win32file.GetVolumePathName(str(path.parent))
            if not win32api.GetVolumeInformation(volume)[3] & win32con.FILE_PERSISTENT_ACLS:
                raise BackendFailedError("WiFi 密码临时文件所在卷不支持访问控制")
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            try:
                user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            finally:
                token.Close()
            sid = win32security.ConvertSidToStringSid(user)
            descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)", win32security.SDDL_REVISION_1,
            )
            attributes = win32security.SECURITY_ATTRIBUTES()
            attributes.SECURITY_DESCRIPTOR = descriptor
            attributes.bInheritHandle = False
            handle = win32file.CreateFile(
                str(path), win32con.GENERIC_WRITE, 0, attributes,
                win32con.CREATE_NEW, win32con.FILE_ATTRIBUTE_TEMPORARY, None,
            )
            created = True
            try:
                win32file.WriteFile(handle, content)
            finally:
                handle.Close()
        yield str(path)
    finally:
        if created:
            path.unlink()


def _profile_xml(ssid, password, name):
    root = ET.Element("WLANProfile", xmlns="http://www.microsoft.com/networking/WLAN/profile/v1")
    ET.SubElement(root, "name").text = name
    config = ET.SubElement(root, "SSIDConfig")
    network = ET.SubElement(config, "SSID")
    ET.SubElement(network, "hex").text = ssid.encode("utf-8").hex().upper()
    ET.SubElement(network, "name").text = ssid
    ET.SubElement(root, "connectionType").text = "ESS"
    ET.SubElement(root, "connectionMode").text = "manual"
    security = ET.SubElement(ET.SubElement(root, "MSM"), "security")
    auth = ET.SubElement(security, "authEncryption")
    ET.SubElement(auth, "authentication").text = "WPA2PSK"
    ET.SubElement(auth, "encryption").text = "AES"
    ET.SubElement(auth, "useOneX").text = "false"
    key = ET.SubElement(security, "sharedKey")
    ET.SubElement(key, "keyType").text = "passPhrase"
    ET.SubElement(key, "protected").text = "false"
    ET.SubElement(key, "keyMaterial").text = password
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class WifiBackend:
    def __init__(self, runner=None):
        self._runner = runner or default_runner

    @contextmanager
    def _interfaces(self):
        with NATIVE_LOCK:
            try:
                wlan = w.wlanapi()
            except (OSError, AttributeError, RuntimeError) as error:
                raise BackendMissingError(f"加载 Windows 无线接口失败：{error}") from error
            handle = ctypes.c_void_p()
            version = w.WLAN_DWORD()
            _check(wlan.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle)), "WlanOpenHandle")
            interfaces = ctypes.c_void_p()
            try:
                _check(wlan.WlanEnumInterfaces(handle, None, ctypes.byref(interfaces)), "WlanEnumInterfaces")
                yield wlan, handle, _items(interfaces, w.WlanInterfaceInfo)
            finally:
                if interfaces.value:
                    wlan.WlanFreeMemory(interfaces)
                wlan.WlanCloseHandle(handle, None)

    def _identity(self, interface):
        guid = str(uuid.UUID(bytes_le=bytes(interface.guid)))
        try:
            iphlp = w.iphlpapi()
        except (OSError, AttributeError, RuntimeError) as error:
            raise BackendMissingError(f"加载 Windows 网卡接口失败：{error}") from error
        luid = ctypes.c_uint64()
        alias = ctypes.create_unicode_buffer(257)
        _check(iphlp.ConvertInterfaceGuidToLuid(ctypes.byref(interface.guid), ctypes.byref(luid)), "ConvertInterfaceGuidToLuid")
        _check(iphlp.ConvertInterfaceLuidToAlias(ctypes.byref(luid), alias, len(alias)), "ConvertInterfaceLuidToAlias")
        return alias.value, guid

    def _matches(self, record, interface):
        if interface is None:
            return True
        guid = str(uuid.UUID(bytes_le=bytes(record.guid)))
        if interface.strip("{}").lower() == guid.lower():
            return True
        return self._identity(record)[0].lower() == interface.lower()

    def _select(self, interface):
        with self._interfaces() as (_, _, records):
            for record in records:
                if self._matches(record, interface):
                    return self._identity(record)
        raise BackendMissingError("未发现所选 WiFi 网卡")

    def current_ssid(self, interface=None) -> str | None:
        with self._interfaces() as (wlan, handle, records):
            found = interface is None
            for record in records:
                if not self._matches(record, interface):
                    continue
                found = True
                if record.state != w.WLAN_CONNECTED:
                    continue
                data = ctypes.c_void_p()
                size = w.WLAN_DWORD()
                value_type = w.WLAN_DWORD()
                try:
                    status = wlan.WlanQueryInterface(
                        handle, ctypes.byref(record.guid), w.WLAN_CURRENT_CONNECTION, None,
                        ctypes.byref(size), ctypes.byref(data), ctypes.byref(value_type),
                    )
                    if status == w.WLAN_INVALID_STATE:
                        continue
                    _check(status, "WlanQueryInterface")
                    if not data.value or size.value < ctypes.sizeof(w.WlanConnectionInfo):
                        raise BackendFailedError("Windows 无线连接数据无效")
                    connection = ctypes.cast(data, ctypes.POINTER(w.WlanConnectionInfo)).contents
                    if connection.state != w.WLAN_CONNECTED:
                        continue
                    ssid = connection.association.ssid
                    if ssid.length > 32:
                        raise BackendFailedError("Windows 无线 SSID 长度无效")
                    return bytes(ssid.data[:ssid.length]).decode("utf-8", errors="replace") or None
                finally:
                    if data.value:
                        wlan.WlanFreeMemory(data)
            if not found:
                raise BackendMissingError("未发现所选 WiFi 网卡")
            return None

    def _saved_profile(self, ssid, interface):
        target = ssid.encode("utf-8")
        with self._interfaces() as (wlan, handle, records):
            for record in records:
                if not self._matches(record, interface):
                    continue
                profiles = ctypes.c_void_p()
                try:
                    _check(wlan.WlanGetProfileList(handle, ctypes.byref(record.guid), None, ctypes.byref(profiles)), "WlanGetProfileList")
                    for profile in _items(profiles, w.WlanProfileInfo):
                        name = _profile_name(profile)
                        xml = ctypes.c_void_p()
                        flags = w.WLAN_DWORD()
                        access = w.WLAN_DWORD()
                        try:
                            _check(wlan.WlanGetProfile(
                                handle, ctypes.byref(record.guid), name, None,
                                ctypes.byref(xml), ctypes.byref(flags), ctypes.byref(access),
                            ), "WlanGetProfile")
                            if not xml.value:
                                raise BackendFailedError("Windows 无线配置未返回数据")
                            root = ET.fromstring(ctypes.wstring_at(xml))
                            for network in root.findall("./{*}SSIDConfig/{*}SSID"):
                                hex_ssid = network.findtext("{*}hex")
                                matched = bytes.fromhex(hex_ssid) == target if hex_ssid is not None else network.findtext("{*}name") == ssid
                                if matched:
                                    alias, guid = self._identity(record)
                                    return name, alias, guid
                        except (ET.ParseError, ValueError):
                            raise BackendFailedError("Windows 无线配置中的 SSID 数据无效") from None
                        finally:
                            if xml.value:
                                wlan.WlanFreeMemory(xml)
                finally:
                    if profiles.value:
                        wlan.WlanFreeMemory(profiles)
        return None

    def _netsh(self, *args):
        with NATIVE_LOCK:
            try:
                kernel = w.kernel32()
                codepage = kernel.GetConsoleOutputCP() or kernel.GetOEMCP()
            except (OSError, AttributeError, RuntimeError) as error:
                raise BackendFailedError(f"查询 Windows 控制台代码页失败：{error}") from error
        try:
            result = self._runner.run(
                ["netsh", "wlan", *args], timeout=30, encoding=f"cp{codepage}", creationflags=w.CREATE_NO_WINDOW,
            )
        except OSError as error:
            raise BackendFailedError(f"执行 netsh 失败：{error}") from error
        if result.returncode:
            detail = (result.stdout or result.stderr or f"退出码 {result.returncode}").strip()
            raise BackendFailedError(f"WiFi 操作失败：{detail[-400:]}")

    def _wait(self, expected, interface):
        deadline = time.monotonic() + 20
        actual = None
        while time.monotonic() < deadline:
            actual = self.current_ssid(interface)
            if actual == expected:
                return
            time.sleep(0.4)
        raise BackendFailedError(f"请求 WiFi 状态 {expected!r}，回读为 {actual!r}")

    def connect(self, ssid, password=None, interface=None) -> dict:
        if not isinstance(ssid, str) or not ssid:
            raise BackendInvalidError("连接 WiFi 时必须填写目标 SSID")
        if password is not None and not isinstance(password, str):
            raise BackendInvalidError("WiFi 密码必须是字符串")
        try:
            saved = self._saved_profile(ssid, interface)
            if saved is None:
                if not password:
                    raise BackendFailedError(f"未找到 SSID {ssid!r} 的已保存 Wi-Fi 配置")
                alias, guid = self._select(interface)
                name = f"NotmyFault-{uuid.uuid4().hex}"
            else:
                name, alias, guid = saved
            if self.current_ssid(guid) != ssid:
                if saved is None:
                    with _private_profile(_profile_xml(ssid, password, name)) as path:
                        self._netsh("add", "profile", f"filename={path}", "user=current")
                        self._netsh("connect", f"name={name}", f"ssid={ssid}", f"interface={alias}")
                else:
                    self._netsh("connect", f"name={name}", f"ssid={ssid}", f"interface={alias}")
                self._wait(ssid, guid)
            return {"ssid": ssid, "connected": True, "method": "netsh"}
        except BackendError as error:
            if password:
                raise type(error)("WiFi 密码连接操作失败，详细输出已隐藏") from None
            raise
        except Exception:
            if password:
                raise BackendFailedError("WiFi 密码连接操作失败，详细输出已隐藏") from None
            raise BackendFailedError("Windows WiFi 连接失败") from None

    def disconnect(self, interface=None) -> dict:
        alias, guid = self._select(interface)
        if self.current_ssid(guid) is not None:
            self._netsh("disconnect", f"interface={alias}")
            self._wait(None, guid)
        return {"ssid": "", "connected": False, "method": "netsh"}
