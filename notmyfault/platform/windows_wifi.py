import ctypes
import uuid

from notmyfault.plugin_api import native_lock

_DWORD = ctypes.c_uint32
_PVOID = ctypes.c_void_p
_GUID = ctypes.c_ubyte * 16


class _InterfaceInfo(ctypes.Structure):
    _fields_ = [("guid", _GUID), ("description", ctypes.c_uint16 * 256), ("state", _DWORD)]


class _SSID(ctypes.Structure):
    _fields_ = [("length", _DWORD), ("data", ctypes.c_ubyte * 32)]


class _Association(ctypes.Structure):
    _fields_ = [
        ("ssid", _SSID), ("bss_type", _DWORD), ("bssid", ctypes.c_ubyte * 6),
        ("phy_type", _DWORD), ("phy_index", _DWORD), ("quality", _DWORD),
        ("rx_rate", _DWORD), ("tx_rate", _DWORD),
    ]


class _Security(ctypes.Structure):
    _fields_ = [
        ("enabled", _DWORD), ("one_x_enabled", _DWORD),
        ("auth_algorithm", _DWORD), ("cipher_algorithm", _DWORD),
    ]


class _ConnectionInfo(ctypes.Structure):
    _fields_ = [
        ("state", _DWORD), ("mode", _DWORD), ("profile_name", ctypes.c_uint16 * 256),
        ("association", _Association), ("security", _Security),
    ]


def current_ssid(interface_guid: str | None = None) -> str | None:
    with native_lock():
        try:
            wlan = ctypes.WinDLL("wlanapi")
        except OSError:
            return None
        signatures = {
            "WlanOpenHandle": [_DWORD, _PVOID, ctypes.POINTER(_DWORD), ctypes.POINTER(_PVOID)],
            "WlanCloseHandle": [_PVOID, _PVOID],
            "WlanEnumInterfaces": [_PVOID, _PVOID, ctypes.POINTER(_PVOID)],
            "WlanQueryInterface": [
                _PVOID, ctypes.POINTER(_GUID), _DWORD, _PVOID,
                ctypes.POINTER(_DWORD), ctypes.POINTER(_PVOID), ctypes.POINTER(_DWORD),
            ],
        }
        for name, arguments in signatures.items():
            function = getattr(wlan, name)
            function.argtypes = arguments
            function.restype = _DWORD
        wlan.WlanFreeMemory.argtypes = [_PVOID]
        wlan.WlanFreeMemory.restype = None

        handle = _PVOID()
        version = _DWORD()
        if wlan.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle)):
            return None
        interfaces = _PVOID()
        try:
            if wlan.WlanEnumInterfaces(handle, None, ctypes.byref(interfaces)) or not interfaces.value:
                return None
            count = ctypes.cast(interfaces, ctypes.POINTER(_DWORD))[0]
            records = ctypes.cast(
                interfaces.value + 8, ctypes.POINTER(_InterfaceInfo * count),
            ).contents
            found = interface_guid is None
            for interface in records:
                guid = str(uuid.UUID(bytes_le=bytes(interface.guid)))
                if interface_guid is not None and guid.lower() != interface_guid.lower():
                    continue
                found = True
                if interface.state != 1:
                    continue
                data = _PVOID()
                size = _DWORD()
                value_type = _DWORD()
                try:
                    status = wlan.WlanQueryInterface(
                        handle, ctypes.byref(interface.guid), 7, None,
                        ctypes.byref(size), ctypes.byref(data), ctypes.byref(value_type),
                    )
                    if status == 5023:
                        continue
                    if status or not data.value or size.value < ctypes.sizeof(_ConnectionInfo):
                        return None
                    connection = ctypes.cast(data, ctypes.POINTER(_ConnectionInfo)).contents
                    if connection.state != 1:
                        continue
                    ssid = connection.association.ssid
                    if ssid.length > 32:
                        return None
                    return bytes(ssid.data[:ssid.length]).decode("utf-8", errors="replace")
                finally:
                    if data.value:
                        wlan.WlanFreeMemory(data)
            return "" if found else None
        finally:
            if interfaces.value:
                wlan.WlanFreeMemory(interfaces)
            wlan.WlanCloseHandle(handle, None)
