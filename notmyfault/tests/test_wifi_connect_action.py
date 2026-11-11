"""WiFi 动作的参数、门面调用与 Windows 无线配置匹配。"""

import ctypes
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from notmyfault.actions.wifi_connect import action
from notmyfault.platform.errors import BackendError
from notmyfault.platform.windows import wifi
from notmyfault.security.plugin_schema import validate_plugin_meta


PLUGIN_ROOT = Path(action.__file__).parent


def test_manifest_is_valid_cross_platform_action():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    valid, errors = validate_plugin_meta(meta, "action")
    assert valid, errors
    assert "platforms" not in meta
    assert meta["requires_capabilities"] == ["wifi.control"]
    assert {output["name"] for output in meta["outputs"]} == {"ssid", "connected", "method"}
    password = next(param for param in meta["params"] if param["name"] == "password")
    assert password["sensitive"] is True


@pytest.mark.parametrize("params", [{"action": "connect"}, {"action": "connect", "ssid": ""}])
def test_connect_requires_ssid(params):
    with pytest.raises(ValueError, match="必须填写目标 SSID"):
        action.run_with_context({}, params, {})


def test_unknown_action_rejected():
    with pytest.raises(ValueError, match="不支持的 WiFi 操作"):
        action.run_with_context({}, {"action": "scan"}, {})




class WlanFixture:
    def __init__(self, monkeypatch, *, target, scenario):
        self.target = target
        self.scenario = scenario
        self.allocations = {}
        self.closed = []
        self.freed = []
        self.profile_name = '保存:网络\\2'
        self.w = wifi.w
        self.library = SimpleNamespace(
            WlanOpenHandle=self.open_handle, WlanCloseHandle=lambda *_: self.closed.append(True) or 0,
            WlanEnumInterfaces=self.interfaces, WlanFreeMemory=self.free,
            WlanGetProfileList=self.profiles, WlanGetProfile=self.profile,
            WlanQueryInterface=self.query,
        )
        monkeypatch.setattr(self.w, "wlanapi", lambda: self.library)
        monkeypatch.setattr(self.w, "iphlpapi", lambda: SimpleNamespace(
            ConvertInterfaceGuidToLuid=self.luid, ConvertInterfaceLuidToAlias=self.alias,
        ))

    def allocate(self, output, buffer):
        address = ctypes.addressof(buffer)
        self.allocations[address] = buffer
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = address

    def records(self, output, record_type, records):
        class Records(ctypes.Structure):
            _fields_ = [("count", ctypes.c_uint32), ("index", ctypes.c_uint32), ("items", record_type * len(records))]
        self.allocate(output, Records(len(records), 0, (record_type * len(records))(*records)))

    def free(self, pointer):
        self.freed.append(pointer.value)
        self.allocations.pop(pointer.value)

    def open_handle(self, _version, _reserved, version, handle):
        ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return 5 if self.scenario == "open_denied" else 0

    def interfaces(self, _handle, _reserved, output):
        records = [self.w.WlanInterfaceInfo(), self.w.WlanInterfaceInfo()]
        for index, record in enumerate(records, 1):
            record.guid[0] = index
            record.state = 1
        self.records(output, self.w.WlanInterfaceInfo, records)
        return 0

    def profiles(self, _handle, _guid, _reserved, output):
        records = []
        for name in [self.target, self.profile_name]:
            record = self.w.WlanProfileInfo()
            encoded = name.encode("utf-16-le")
            for index in range(0, len(encoded), 2):
                record.name[index // 2] = int.from_bytes(encoded[index:index + 2], "little")
            records.append(record)
        self.records(output, self.w.WlanProfileInfo, records)
        return 0

    def profile(self, _handle, guid, name, _reserved, output, _flags, _access):
        index = ctypes.cast(guid, ctypes.POINTER(self.w.WLAN_GUID)).contents[0]
        actual = self.target if index == 2 and name == self.profile_name else "Unrelated"
        if self.scenario == "missing":
            actual = "Unrelated"
        root = ET.Element("WLANProfile", xmlns="http://www.microsoft.com/networking/WLAN/profile/v1")
        config = ET.SubElement(root, "SSIDConfig")
        for value in (["Other", actual] if self.scenario == "multi" else [actual]):
            network = ET.SubElement(config, "SSID")
            if self.scenario != "name" or name == self.target:
                ET.SubElement(network, "hex").text = value.encode("utf-8").hex()
            ET.SubElement(network, "name").text = self.target if name == self.target else value
        xml = ET.tostring(root, encoding="unicode")
        if self.scenario == "invalid_xml":
            xml = "<invalid"
        if self.scenario == "invalid_hex":
            xml = "<WLANProfile><SSIDConfig><SSID><hex>ZZ</hex></SSID></SSIDConfig></WLANProfile>"
        self.allocate(output, ctypes.create_unicode_buffer(xml))
        return 0

    def luid(self, guid, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64))[0] = ctypes.cast(guid, ctypes.POINTER(self.w.WLAN_GUID)).contents[0]
        return 0

    def alias(self, luid, output, _length):
        output.value = f"Wi-Fi {ctypes.cast(luid, ctypes.POINTER(ctypes.c_uint64))[0]}"
        return 0

    def query(self, _handle, guid, _opcode, _reserved, size, output, _type):
        if self.scenario == "query_failed":
            return 123
        if self.scenario == "disconnected":
            return self.w.WLAN_INVALID_STATE
        record = self.w.WlanConnectionInfo()
        record.state = self.w.WLAN_CONNECTED
        raw = self.target.encode("utf-8")
        record.association.ssid.length = len(raw)
        record.association.ssid.data[:len(raw)] = raw
        self.allocate(output, record)
        ctypes.cast(size, ctypes.POINTER(self.w.WLAN_DWORD))[0] = ctypes.sizeof(record)
        return 0


@pytest.mark.parametrize("scenario", ["hex", "name", "multi", "missing", "invalid_xml", "invalid_hex", "open_denied"])
def test_saved_profiles_match_utf8_ssid_and_release_memory(monkeypatch, scenario):
    target = '"家中:WiFi"'
    native = WlanFixture(monkeypatch, target=target, scenario=scenario)
    backend = wifi.WifiBackend()
    if scenario in ("invalid_xml", "invalid_hex", "open_denied"):
        with pytest.raises(BackendError) as error:
            backend._saved_profile(target, "Wi-Fi 2")
        assert error.value.kind == ("permission_denied" if scenario == "open_denied" else "failed")
    else:
        result = backend._saved_profile(target, "Wi-Fi 2")
        if scenario == "missing":
            assert result is None
        else:
            assert result == (native.profile_name, "Wi-Fi 2", "00000002-0000-0000-0000-000000000000")
    assert not native.allocations
    assert bool(native.closed) is (scenario != "open_denied")


@pytest.mark.parametrize("scenario", ["hex", "disconnected", "query_failed"])
def test_current_ssid_distinguishes_query_failure(monkeypatch, scenario):
    target = '家中:WiFi\\2'
    native = WlanFixture(monkeypatch, target=target, scenario=scenario)
    backend = wifi.WifiBackend()
    if scenario == "query_failed":
        with pytest.raises(BackendError) as error:
            backend.current_ssid("00000002-0000-0000-0000-000000000000")
        assert error.value.kind == "failed"
    else:
        assert backend.current_ssid("00000002-0000-0000-0000-000000000000") == (None if scenario == "disconnected" else target)
    assert not native.allocations
    assert native.closed


@pytest.mark.parametrize("failure", [False, True])
def test_windows_secret_file_has_private_acl_before_write(monkeypatch, tmp_path, failure):
    opened = []
    closed = []
    descriptor = object()
    monkeypatch.setattr(wifi.tempfile, "gettempdir", lambda: str(tmp_path))

    def convert_sddl(sddl, revision):
        assert sddl == "D:P(A;;FA;;;S-1-5-21-test)(A;;FA;;;SY)"
        assert revision == 1
        return descriptor

    def create_file(path, access, sharing, attributes, creation, flags, template):
        assert attributes.SECURITY_DESCRIPTOR is descriptor
        assert attributes.bInheritHandle is False
        assert (access, sharing, creation, flags, template) == (2, 0, 1, 256, None)
        assert not Path(path).exists()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        opened.append(Path(path))
        return SimpleNamespace(
            fd=fd, security_descriptor=attributes.SECURITY_DESCRIPTOR,
            Close=lambda: (os.close(fd), closed.append(path)),
        )

    def write_file(handle, content):
        assert handle.security_descriptor is descriptor
        assert os.fstat(handle.fd).st_size == 0
        assert content == b"<profile>example-pass</profile>"
        os.write(handle.fd, content)
        if failure:
            raise OSError("write failed")

    modules = {
        "win32api": SimpleNamespace(GetCurrentProcess=lambda: 100, GetVolumeInformation=lambda _: ("", 0, 255, 8, "NTFS")),
        "win32con": SimpleNamespace(TOKEN_QUERY=8, FILE_PERSISTENT_ACLS=8, GENERIC_WRITE=2, CREATE_NEW=1, FILE_ATTRIBUTE_TEMPORARY=256),
        "win32file": SimpleNamespace(CreateFile=create_file, WriteFile=write_file, GetVolumePathName=lambda _: "C:\\"),
        "win32security": SimpleNamespace(
            OpenProcessToken=lambda *_: SimpleNamespace(Close=lambda: None),
            GetTokenInformation=lambda *_: ("user", None), TokenUser=1,
            ConvertSidToStringSid=lambda _: "S-1-5-21-test",
            ConvertStringSecurityDescriptorToSecurityDescriptor=convert_sddl,
            SDDL_REVISION_1=1, SECURITY_ATTRIBUTES=SimpleNamespace,
        ),
    }
    for name, value in modules.items():
        monkeypatch.setitem(sys.modules, name, value)
    if failure:
        with pytest.raises(OSError, match="write failed"):
            with wifi._private_profile(b"<profile>example-pass</profile>"):
                pytest.fail("写入失败后不能读取临时配置")
    else:
        with wifi._private_profile(b"<profile>example-pass</profile>") as path:
            assert Path(path).read_bytes() == b"<profile>example-pass</profile>"
    assert opened and closed
    assert all(not path.exists() for path in opened)
