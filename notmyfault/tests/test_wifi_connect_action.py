"""wifi_connect 动作的参数、系统调用和状态确认测试。"""

import importlib.util
import ctypes
import json
import os
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from notmyfault.security.plugin_schema import validate_plugin_meta


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "wifi_connect"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("wifi_connect_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed(*, returncode=0, stdout=b"", stderr=b""):
    if isinstance(stdout, str):
        stdout = stdout.encode()
    if isinstance(stderr, str):
        stderr = stderr.encode()
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def completed_text(*, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_manifest_is_valid_cross_platform_action():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    valid, errors = validate_plugin_meta(meta, "action")
    assert valid, errors
    assert meta["id"] == "wifi_connect"
    assert meta["platforms"] == ["windows", "linux"]
    assert "external_binary" in meta["permissions"]
    assert "security" not in meta
    password = next(param for param in meta["params"] if param["name"] == "password")
    assert password["sensitive"] is True


@pytest.mark.parametrize("params", [{"action": "connect"}, {"action": "connect", "ssid": ""}])
def test_connect_requires_ssid(params):
    module = load_module()
    with pytest.raises(ValueError, match="必须填写目标 SSID"):
        module.run_with_context({}, params, {})


def test_unknown_action_rejected():
    module = load_module()
    with pytest.raises(ValueError, match="不支持的 WiFi 操作"):
        module.run_with_context({}, {"action": "scan"}, {})


@pytest.mark.parametrize("ssid", ["HomeWiFi", '"家中:WiFi"', r"Lab\WiFi", "Cafe:WiFi", 'Home"', " Home ", " ", "\u3000Home\u3000"])
@pytest.mark.parametrize("profile_kind", [
    "same_name", "renamed", "missing", "query_failed", "adapter_missing",
    "already_selected", "wrong_adapter_after_connect",
])
def test_windows_connect_uses_netsh_and_waits_for_ssid(monkeypatch, ssid, profile_kind):
    from notmyfault.platform import windows_wifi

    module = load_module()
    calls = []
    states = ["OldNet", "OldNet", "OldNet", ssid]
    if profile_kind == "already_selected":
        states = ["OldNet", ssid]
    interface_guid = "00000002-0000-0000-0000-000000000000"
    profile_name = ssid if profile_kind == "same_name" else '已保存:网络\\2'

    def saved_profile(target):
        assert target == ssid
        if profile_kind == "query_failed":
            raise RuntimeError("Windows 无线配置查询失败")
        if profile_kind == "missing":
            return None
        return profile_name, "Wi-Fi 2", interface_guid

    monkeypatch.setattr(module, "_saved_profile_windows", saved_profile, raising=False)

    def query(guid=None):
        current = states.pop(0) if states else ssid
        if guid is None:
            return "OtherAdapter"
        assert guid == interface_guid
        if profile_kind == "adapter_missing":
            return None
        return "OldNet" if profile_kind == "wrong_adapter_after_connect" else current

    monkeypatch.setattr(windows_wifi, "current_ssid", query)

    def fake_run(command, **kwargs):
        calls.append(command)
        assert command[:3] == ["netsh", "wlan", "connect"]
        return completed(stdout=b"Connection request was completed successfully.\n")

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_decode_netsh", lambda raw: raw.decode("utf-8", errors="replace"))
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda _seconds: clock.__setitem__(0, clock[0] + 1),
    ))

    if profile_kind in ("missing", "query_failed", "adapter_missing"):
        with pytest.raises(RuntimeError):
            module.run_with_context({}, {"action": "connect", "ssid": ssid}, {})
        assert not any(command[:3] == ["netsh", "wlan", "connect"] for command in calls)
        return
    if profile_kind == "wrong_adapter_after_connect":
        with pytest.raises(RuntimeError, match="回读为"):
            module.run_with_context({}, {"action": "connect", "ssid": ssid}, {})
        assert any(command[:3] == ["netsh", "wlan", "connect"] for command in calls)
        return

    result = module.run_with_context({}, {"action": "connect", "ssid": ssid}, {})

    assert result["ssid"] == ssid
    assert result["previous_ssid"] == (ssid if profile_kind == "already_selected" else "OldNet")
    assert result["connected"] is True
    assert result["changed"] is (profile_kind != "already_selected")
    assert result["method"] == "netsh"
    if profile_kind == "already_selected":
        assert not any(command[:3] == ["netsh", "wlan", "connect"] for command in calls)
        return
    connect = next(command for command in calls if command[:3] == ["netsh", "wlan", "connect"])
    assert f"name={profile_name}" in connect
    assert f"ssid={ssid}" in connect
    assert "interface=Wi-Fi 2" in connect


@pytest.mark.parametrize("scenario", [
    "hex", "name", "multi_ssid", "no_namespace", "missing", "invalid_xml", "invalid_hex",
    "WlanOpenHandle", "WlanEnumInterfaces", "WlanGetProfileList", "WlanGetProfile",
    "ConvertInterfaceGuidToLuid", "ConvertInterfaceLuidToAlias",
])
def test_windows_saved_profiles_match_ssid_and_release_native_memory(monkeypatch, scenario):
    module = load_module()
    target = '"家中:WiFi"'
    saved_name = r"保存过:网络\2"
    allocations = {}
    closed = []

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8),
        ]

    class InterfaceInfo(ctypes.Structure):
        _fields_ = [("guid", GUID), ("description", ctypes.c_wchar * 256), ("state", ctypes.c_uint32)]

    class ProfileInfo(ctypes.Structure):
        _fields_ = [("name", ctypes.c_wchar * 256), ("flags", ctypes.c_uint32)]

    def allocate(output, buffer):
        address = ctypes.addressof(buffer)
        allocations[address] = buffer
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = address

    def allocate_list(output, item_type, records):
        class RecordList(ctypes.Structure):
            _fields_ = [
                ("count", ctypes.c_uint32), ("index", ctypes.c_uint32),
                ("records", item_type * len(records)),
            ]
        allocate(output, RecordList(len(records), 0, (item_type * len(records))(*records)))

    def profile_xml(actual, *, deceptive_name=False):
        root = ET.Element("WLANProfile", {} if scenario == "no_namespace" else {
            "xmlns": "http://www.microsoft.com/networking/WLAN/profile/v1",
        })
        config = ET.SubElement(root, "SSIDConfig")
        names = ["Other", actual] if scenario == "multi_ssid" else [actual]
        for name in names:
            network = ET.SubElement(config, "SSID")
            if scenario != "name" or deceptive_name:
                ET.SubElement(network, "hex").text = name.encode("utf-8").hex()
            ET.SubElement(network, "name").text = target if deceptive_name else name
        return ET.tostring(root, encoding="unicode")

    def open_handle(_version, _reserved, version, handle):
        ctypes.cast(version, ctypes.POINTER(ctypes.c_uint32))[0] = 2
        ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return 0

    def enumerate_interfaces(_handle, _reserved, output):
        records = [InterfaceInfo(), InterfaceInfo()]
        records[0].guid.Data1 = 1
        records[1].guid.Data1 = 2
        allocate_list(output, InterfaceInfo, records)
        return 0

    def get_profiles(_handle, guid, _reserved, output):
        index = ctypes.cast(guid, ctypes.POINTER(GUID)).contents.Data1
        names = ["Elsewhere"] if index == 1 else [target, saved_name]
        allocate_list(output, ProfileInfo, [ProfileInfo(name, 0) for name in names])
        return 0

    def get_profile(_handle, _guid, name, _reserved, output, flags, _access):
        assert ctypes.cast(flags, ctypes.POINTER(ctypes.c_uint32))[0] == 0
        actual = target if name == saved_name and scenario != "missing" else "Unrelated"
        xml = profile_xml(actual, deceptive_name=name == target)
        if name == saved_name:
            if scenario == "invalid_xml":
                xml = "<invalid"
            elif scenario == "invalid_hex":
                xml = "<WLANProfile><SSIDConfig><SSID><hex>ZZ</hex></SSID></SSIDConfig></WLANProfile>"
        allocate(output, ctypes.create_unicode_buffer(xml))
        return 0

    def to_luid(guid, output):
        assert ctypes.cast(guid, ctypes.POINTER(GUID)).contents.Data1 == 2
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64))[0] = 456
        return 0

    def to_alias(luid, output, length):
        assert ctypes.cast(luid, ctypes.POINTER(ctypes.c_uint64))[0] == 456
        assert length >= len("Wi-Fi 2") + 1
        output.value = "Wi-Fi 2"
        return 0

    def free_memory(pointer):
        allocations.pop(pointer.value)

    def close_handle(handle, _reserved):
        closed.append(handle.value)
        return 0

    def function(name, implementation):
        def call(*args):
            if scenario == name:
                return 5
            return implementation(*args)
        return call

    wlan = SimpleNamespace(**{name: function(name, implementation) for name, implementation in {
        "WlanOpenHandle": open_handle, "WlanEnumInterfaces": enumerate_interfaces,
        "WlanGetProfileList": get_profiles, "WlanGetProfile": get_profile,
        "WlanFreeMemory": free_memory, "WlanCloseHandle": close_handle,
    }.items()})
    iphlp = SimpleNamespace(
        ConvertInterfaceGuidToLuid=function("ConvertInterfaceGuidToLuid", to_luid),
        ConvertInterfaceLuidToAlias=function("ConvertInterfaceLuidToAlias", to_alias),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name: {"wlanapi": wlan, "iphlpapi": iphlp}[name], raising=False)

    if scenario.startswith(("Wlan", "Convert", "invalid")):
        with pytest.raises(RuntimeError, match="Windows 无线配置"):
            module._saved_profile_windows(target)
    else:
        expected = None if scenario == "missing" else (
            saved_name, "Wi-Fi 2", "00000002-0000-0000-0000-000000000000",
        )
        assert module._saved_profile_windows(target) == expected
    assert allocations == {}
    assert closed == ([] if scenario == "WlanOpenHandle" else [123])
    for library in (wlan, iphlp):
        for name, native_function in vars(library).items():
            assert native_function.argtypes
            assert native_function.restype is (None if name == "WlanFreeMemory" else ctypes.c_uint32)


def test_windows_connect_skips_when_already_on_target(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_current_ssid_windows", lambda: "HomeWiFi")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("不应再调用 netsh"),
    )
    result = module.run_with_context({}, {"action": "connect", "ssid": "HomeWiFi"}, {})
    assert result == {
        "action": "connect",
        "ssid": "HomeWiFi",
        "previous_ssid": "HomeWiFi",
        "connected": True,
        "changed": False,
        "method": "netsh",
    }


@pytest.mark.parametrize("ssid", [" Home ", " ", "家😀\x00"])
@pytest.mark.parametrize("scenario", [
    "connected", "disconnected", "transition", "query_failure",
    "enumeration_failure", "open_failure", "short_data", "invalid_length",
])
def test_windows_current_ssid_preserves_bytes_and_releases_native_memory(monkeypatch, ssid, scenario):
    from notmyfault.platform import windows_wifi

    allocations = {}
    closed = []

    def allocate(output, data):
        buffer = ctypes.create_string_buffer(bytes(data))
        address = ctypes.addressof(buffer)
        allocations[address] = buffer
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = address

    def open_handle(version, reserved, output_version, output_handle):
        assert version == 2 and reserved is None
        if scenario == "open_failure":
            return 5
        ctypes.cast(output_handle, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return 0

    def enumerate_interfaces(handle, reserved, output):
        data = bytearray(8 + 532 * 2)
        struct.pack_into("<II", data, 0, 2, 0)
        struct.pack_into("<I", data, 8, 1)
        struct.pack_into("<I", data, 8 + 532, 2)
        struct.pack_into("<I", data, 8 + 532 + 528, 0 if scenario == "disconnected" else 1)
        allocate(output, data)
        return 5 if scenario == "enumeration_failure" else 0

    def query(handle, guid, opcode, reserved, output_size, output, value_type):
        assert handle.value == 123 and opcode == 7 and reserved is None
        assert ctypes.string_at(guid, 16) == bytes.fromhex("02000000000000000000000000000000")
        encoded = ssid.encode("utf-8")
        data = bytearray(604)
        struct.pack_into("<I", data, 0, 1)
        struct.pack_into("<I", data, 520, 33 if scenario == "invalid_length" else len(encoded))
        data[524:524 + len(encoded)] = encoded
        allocate(output, data)
        ctypes.cast(output_size, ctypes.POINTER(ctypes.c_uint32))[0] = 603 if scenario == "short_data" else 604
        return {"transition": 5023, "query_failure": 5}.get(scenario, 0)

    def close(handle, reserved):
        closed.append(handle.value)
        return 0

    def free(pointer):
        allocations.pop(pointer.value)

    wlan = SimpleNamespace(
        WlanOpenHandle=open_handle, WlanEnumInterfaces=enumerate_interfaces,
        WlanQueryInterface=query, WlanCloseHandle=close, WlanFreeMemory=free,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name: wlan, raising=False)
    expected = ssid if scenario == "connected" else "" if scenario in ("disconnected", "transition") else None
    assert windows_wifi.current_ssid() == expected
    assert windows_wifi.current_ssid("00000002-0000-0000-0000-000000000000") == expected
    assert windows_wifi.current_ssid("00000009-0000-0000-0000-000000000000") is None
    assert allocations == {}
    assert closed == ([] if scenario == "open_failure" else [123, 123, 123])
    for name, function in vars(wlan).items():
        assert function.argtypes
        assert function.restype is (None if name == "WlanFreeMemory" else ctypes.c_uint32)


def test_windows_disconnect(monkeypatch):
    module = load_module()
    states = ["HomeWiFi", "HomeWiFi", ""]

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(
        module,
        "_current_ssid_windows",
        lambda: states.pop(0) if states else "",
    )
    monkeypatch.setattr(module, "_decode_netsh", lambda raw: raw.decode())
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: completed(stdout=b"Disconnection completed."),
    )
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    result = module.run_with_context({}, {"action": "disconnect"}, {})
    assert result["connected"] is False
    assert result["previous_ssid"] == "HomeWiFi"
    assert result["changed"] is True


@pytest.mark.parametrize("ssid", ["HomeWiFi", '"家中:WiFi"', r"Lab\WiFi", "Cafe:WiFi", 'Home"', " Home ", " ", "\u3000Home\u3000"])
@pytest.mark.parametrize("profile_kind", ["same_name", "renamed", "wrong_name_match"])
@pytest.mark.parametrize("locale_env", [
    {"LANG": "C", "LC_ALL": "C"}, {"LANG": "zh_CN.UTF-8"},
    {"LANG": "C", "LC_ALL": "zh_CN.UTF-8"},
])
def test_linux_connect_uses_saved_connection(monkeypatch, ssid, profile_kind, locale_env):
    module = load_module()
    calls = []
    states = ["Cafe", "Cafe", ssid]
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    for key, value in locale_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    saved_uuid = "11111111-1111-1111-1111-111111111111"
    saved_name = ssid if profile_kind == "same_name" else "已保存:网络\\2"
    profiles = [{"name": saved_name, "uuid": saved_uuid, "ssid": ssid}]
    if profile_kind == "wrong_name_match":
        profiles.insert(0, {"name": ssid, "uuid": "22222222-2222-2222-2222-222222222222", "ssid": "Unrelated"})

    def fake_run(command, **kwargs):
        calls.append(command)
        environment = kwargs.get("env", os.environ)
        assert environment["DBUS_SESSION_BUS_ADDRESS"] == os.environ["DBUS_SESSION_BUS_ADDRESS"]
        if command[1:4] == ["-t", "-f", "ACTIVE,SSID"]:
            current = states.pop(0) if states else ssid
            encoded = current.replace("\\", "\\\\").replace(":", "\\:")
            english = (environment.get("LC_ALL") or environment.get("LANG")) == "C"
            active = "yes" if english else "是"
            return completed_text(stdout=f"{active}:{encoded}\n")
        if command[1:4] == ["-t", "-f", "UUID,TYPE"]:
            rows = []
            for profile in profiles:
                rows.append(f"{profile['uuid']}:802-11-wireless")
            rows.append("33333333-3333-3333-3333-333333333333:802-3-ethernet")
            return completed_text(stdout="\n".join(rows) + "\n")
        if "-g" in command:
            assert command[command.index("-g") + 1] == "802-11-wireless.ssid"
            assert command[-2] == "uuid"
            profile = next(profile for profile in profiles if profile["uuid"] == command[-1])
            return completed_text(stdout=profile["ssid"] + "\n")
        if command[1:3] == ["connection", "up"]:
            return completed_text(stdout="Connection successfully activated\n")
        return completed_text()

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda _seconds: clock.__setitem__(0, clock[0] + 1),
    ))

    result = module.run_with_context({}, {"action": "connect", "ssid": ssid}, {})
    assert result["method"] == "nmcli"
    assert result["ssid"] == ssid
    assert result["previous_ssid"] == "Cafe"
    assert result["changed"] is True
    activation = next(command for command in calls if command[1:3] == ["connection", "up"] or command[1:4] == ["device", "wifi", "connect"])
    assert activation[1:3] == ["connection", "up"]
    assert activation[3] == "uuid"
    activated = next(profile for profile in profiles if profile["uuid"] == activation[4])
    assert activated["ssid"] == ssid
    assert activated["uuid"] == saved_uuid
    assert dict(os.environ).get("LC_ALL") == locale_env.get("LC_ALL")


@pytest.mark.parametrize("query_failure", [None, "listing", "profile"])
def test_linux_connect_with_password_for_new_network(monkeypatch, query_failure):
    module = load_module()
    calls = []
    states = ["", "", "NewNet"]

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:4] in (["-t", "-f", "NAME,TYPE"], ["-t", "-f", "UUID,TYPE"]):
            if query_failure == "listing":
                return completed_text(returncode=1, stderr="配置列表查询失败")
            return completed_text(stdout="22222222-2222-2222-2222-222222222222:802-11-wireless\n" if query_failure == "profile" else "")
        if "-g" in command:
            return completed_text(returncode=1, stderr="配置内容查询失败")
        if command[1:4] == ["device", "wifi", "connect"]:
            return completed_text(stdout="Device 'wlan0' successfully activated\n")
        return completed_text()

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(
        module,
        "_current_ssid_linux",
        lambda: states.pop(0) if states else "NewNet",
    )
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    if query_failure:
        with pytest.raises(RuntimeError):
            module.run_with_context({}, {"action": "connect", "ssid": "NewNet", "password": "secret"}, {})
        assert not any(command[1:3] == ["connection", "up"] or command[1:4] == ["device", "wifi", "connect"] for command in calls)
        return
    result = module.run_with_context(
        {}, {"action": "connect", "ssid": "NewNet", "password": "secret"}, {}
    )
    assert result["ssid"] == "NewNet"
    connect_cmd = calls[1]
    assert connect_cmd[1:] == [
        "device", "wifi", "connect", "NewNet", "password", "secret",
    ]


def test_linux_password_warning_printed_once(monkeypatch, capsys):
    module = load_module()
    states = ["", "", "NewNet", "", "", "NewNet"]

    def fake_run(command, **kwargs):
        return completed_text(stdout="Device 'wlan0' successfully activated\n")

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(
        module, "_current_ssid_linux", lambda: states.pop(0) if states else "NewNet"
    )
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    params = {"action": "connect", "ssid": "NewNet", "password": "secret"}
    assert module.run_with_context({}, params, {})["ssid"] == "NewNet"
    assert module.run_with_context({}, params, {})["ssid"] == "NewNet"

    captured = capsys.readouterr()
    assert captured.err.count("进程命令行") == 1


@pytest.mark.parametrize("other_connected", [False, True])
@pytest.mark.parametrize("query_failure", [None, "initial", "verification"])
def test_linux_disconnect_uses_wifi_device(monkeypatch, other_connected, query_failure):
    module = load_module()
    networks = {"wlan0": "HomeWiFi", "wlan1": "OtherNet" if other_connected else ""}
    calls = []

    def control_calls():
        return [command for command in calls if command[1:3] == ["device", "disconnect"]]

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:4] == ["-t", "-f", "ACTIVE,SSID"]:
            if query_failure == "initial" or query_failure == "verification" and control_calls():
                return completed_text(returncode=1, stderr="查询失败")
            selected = [networks[command[-1]]] if "ifname" in command else list(networks.values())
            return completed_text(stdout="".join("yes:" + ssid + "\n" for ssid in selected if ssid))
        if command[1:4] == ["-t", "-f", "DEVICE,TYPE,STATE"]:
            return completed_text(stdout="wlan0:wifi:connected\nwlan1:wifi:" + ("connected" if other_connected else "disconnected") + "\neth0:ethernet:connected\n")
        if command[1:3] == ["device", "disconnect"]:
            networks[command[3]] = ""
            return completed_text(stdout="Device 'wlan0' successfully disconnected.\n")
        pytest.fail("不支持的模拟命令")

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    ))

    if query_failure:
        with pytest.raises(RuntimeError):
            module.run_with_context({}, {"action": "disconnect"}, {})
    else:
        result = module.run_with_context({}, {"action": "disconnect"}, {})
        assert result["changed"] is True
        assert result["connected"] is False
        assert result["previous_ssid"] == "HomeWiFi"
    assert networks == {
        "wlan0": "HomeWiFi" if query_failure == "initial" else "",
        "wlan1": "OtherNet" if other_connected else "",
    }
    assert [command[1:] for command in control_calls()] == (
        [] if query_failure == "initial" else [["device", "disconnect", "wlan0"]]
    )
