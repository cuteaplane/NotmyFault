from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
from functools import lru_cache

from notmyfault.platform.errors import BackendMissingError

RECT = wt.RECT
DWORD = wt.DWORD
MSG = wt.MSG
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
VK_RWIN = 0x5C


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("bounds", wt.RECT), ("work", wt.RECT),
                ("flags", wt.DWORD), ("device", wt.WCHAR * 32)]


class PhysicalMonitor(ctypes.Structure):
    _fields_ = [("handle", wt.HANDLE), ("description", wt.WCHAR * 128)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD),
        ("dwExtraInfo", wt.WPARAM),
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG),
        ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", wt.WPARAM),
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD), ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]

class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT),
    ]

class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


_callback = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WNDENUMPROC = _callback(wt.BOOL, wt.HWND, wt.LPARAM)
MONITORENUMPROC = _callback(wt.BOOL, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)


def _load(name, declarations):
    try:
        api = ctypes.WinDLL(name, use_last_error=True)
    except (OSError, AttributeError) as error:
        raise BackendMissingError(f"Windows 原生库不可用: {name}") from error
    for symbol, (result, arguments) in declarations.items():
        function = getattr(api, symbol, None)
        if function is None and symbol == "SetThreadDpiAwarenessContext":
            continue
        if function is None:
            raise BackendMissingError(f"Windows 缺少原生接口: {symbol}")
        function.restype, function.argtypes = result, arguments
    return api


@lru_cache(maxsize=1)
def user32():
    declarations = {
        "IsWindow": (wt.BOOL, [wt.HWND]),
        "IsWindowVisible": (wt.BOOL, [wt.HWND]),
        "IsIconic": (wt.BOOL, [wt.HWND]),
        "IsZoomed": (wt.BOOL, [wt.HWND]),
        "GetForegroundWindow": (wt.HWND, []),
        "GetAncestor": (wt.HWND, [wt.HWND, wt.UINT]),
        "GetWindowTextLengthW": (ctypes.c_int, [wt.HWND]),
        "GetWindowTextW": (ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
        "GetClassNameW": (ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
        "GetWindowThreadProcessId": (wt.DWORD, [wt.HWND, ctypes.POINTER(wt.DWORD)]),
        "GetWindowRect": (wt.BOOL, [wt.HWND, ctypes.POINTER(wt.RECT)]),
        "EnumWindows": (wt.BOOL, [WNDENUMPROC, wt.LPARAM]),
        "GetWindowLongW": (wt.LONG, [wt.HWND, ctypes.c_int]),
        "SetWindowLongW": (wt.LONG, [wt.HWND, ctypes.c_int, wt.LONG]),
        "ShowWindowAsync": (wt.BOOL, [wt.HWND, ctypes.c_int]),
        "SetForegroundWindow": (wt.BOOL, [wt.HWND]),
        "SetWindowPos": (wt.BOOL, [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wt.UINT]),
        "PostMessageW": (wt.BOOL, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
        "SetLayeredWindowAttributes": (wt.BOOL, [wt.HWND, wt.DWORD, wt.BYTE, wt.DWORD]),
        "GetLayeredWindowAttributes": (wt.BOOL, [wt.HWND, ctypes.POINTER(wt.DWORD),
                                                ctypes.POINTER(wt.BYTE), ctypes.POINTER(wt.DWORD)]),
        "MonitorFromWindow": (wt.HANDLE, [wt.HWND, wt.DWORD]),
        "EnumDisplayMonitors": (wt.BOOL, [wt.HDC, ctypes.POINTER(wt.RECT), MONITORENUMPROC, wt.LPARAM]),
        "GetMonitorInfoW": (wt.BOOL, [wt.HANDLE, ctypes.POINTER(MonitorInfo)]),
        "SetThreadDpiAwarenessContext": (ctypes.c_void_p, [ctypes.c_void_p]),
    }
    declarations.update({
        "OpenClipboard": (wt.BOOL, [wt.HWND]),
        "CloseClipboard": (wt.BOOL, []),
        "IsClipboardFormatAvailable": (wt.BOOL, [wt.UINT]),
        "GetClipboardData": (ctypes.c_void_p, [wt.UINT]),
        "EmptyClipboard": (wt.BOOL, []),
        "SetClipboardData": (ctypes.c_void_p, [wt.UINT, ctypes.c_void_p]),
        "SendInput": (wt.UINT, [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]),
        "FindWindowW": (wt.HWND, [wt.LPCWSTR, wt.LPCWSTR]),
        "SendMessageTimeoutW": (ctypes.c_ssize_t, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_size_t)]),
        "SendNotifyMessageW": (wt.BOOL, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
        "GetSystemMetrics": (ctypes.c_int, [ctypes.c_int]),
        "GetDC": (wt.HDC, [wt.HWND]),
        "ReleaseDC": (ctypes.c_int, [wt.HWND, wt.HDC]),
        "PrintWindow": (wt.BOOL, [wt.HWND, wt.HDC, wt.UINT]),
        "RegisterHotKey": (wt.BOOL, [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]),
        "UnregisterHotKey": (wt.BOOL, [wt.HWND, ctypes.c_int]),
        "PeekMessageW": (wt.BOOL, [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]),
        "GetAsyncKeyState": (ctypes.c_short, [ctypes.c_int]),
        "LockWorkStation": (ctypes.c_int, []),
        "GetLastInputInfo": (ctypes.c_bool, [ctypes.POINTER(LASTINPUTINFO)]),
        "ExitWindowsEx": (ctypes.c_int, [ctypes.c_uint, ctypes.c_uint32]),
        "TranslateMessage": (wt.BOOL, [ctypes.POINTER(POWER_MSG)]),
        "DispatchMessageW": (ctypes.c_ssize_t, [ctypes.POINTER(POWER_MSG)]),
        "DestroyWindow": (wt.BOOL, [wt.HWND]),
        "UnregisterClassW": (wt.BOOL, [wt.LPCWSTR, wt.HINSTANCE]),
        "DefWindowProcW": (ctypes.c_ssize_t, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
        "RegisterClassW": (ctypes.c_ushort, [ctypes.POINTER(POWER_WNDCLASSW)]),
        "CreateWindowExW": (wt.HWND, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wt.HWND, ctypes.c_void_p, wt.HINSTANCE, ctypes.c_void_p]),
        "SystemParametersInfoW": (ctypes.c_int, [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]),
    })
    return _load("user32", declarations)


@lru_cache(maxsize=1)
def kernel32():
    return _load("kernel32", {
        "GlobalAlloc": (ctypes.c_void_p, [wt.UINT, ctypes.c_size_t]),
        "GlobalLock": (ctypes.c_void_p, [ctypes.c_void_p]),
        "GlobalUnlock": (wt.BOOL, [ctypes.c_void_p]),
        "GlobalFree": (ctypes.c_void_p, [ctypes.c_void_p]),
        "GlobalSize": (ctypes.c_size_t, [ctypes.c_void_p]),
        "CreateFileW": (ctypes.c_void_p, [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]),
        "CloseHandle": (ctypes.c_int, [ctypes.c_void_p]),
        "ProcessIdToSessionId": (wt.BOOL, [SESSION_DWORD, ctypes.POINTER(SESSION_DWORD)]),
        "GetTickCount": (ctypes.c_uint32, []),
        "GetSystemPowerStatus": (wt.BOOL, [ctypes.POINTER(SYSTEM_POWER_STATUS)]),
        "GetCurrentProcess": (ctypes.c_void_p, []),
        "SetLastError": (None, [ctypes.c_uint32]),
        "GetLastError": (ctypes.c_uint32, []),
        "GetModuleHandleW": (wt.HMODULE, [wt.LPCWSTR]),
        "GetCurrentThreadId": (wt.DWORD, []),
        "GetConsoleOutputCP": (ctypes.c_uint, []),
        "GetOEMCP": (ctypes.c_uint, []),
    })


@lru_cache(maxsize=1)
def gdi32():
    return _load("gdi32", {
        "CreateCompatibleDC": (wt.HDC, [wt.HDC]),
        "DeleteDC": (wt.BOOL, [wt.HDC]),
        "CreateCompatibleBitmap": (wt.HBITMAP, [wt.HDC, ctypes.c_int, ctypes.c_int]),
        "SelectObject": (wt.HGDIOBJ, [wt.HDC, wt.HGDIOBJ]),
        "DeleteObject": (wt.BOOL, [wt.HGDIOBJ]),
        "BitBlt": (wt.BOOL, [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]),
        "GetDIBits": (ctypes.c_int, [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]),
    })


@lru_cache(maxsize=1)
def dxva2():
    return _load("Dxva2", {
        "GetNumberOfPhysicalMonitorsFromHMONITOR": (wt.BOOL, [wt.HANDLE, ctypes.POINTER(wt.DWORD)]),
        "GetPhysicalMonitorsFromHMONITOR": (wt.BOOL, [wt.HANDLE, wt.DWORD, ctypes.POINTER(PhysicalMonitor)]),
        "GetMonitorBrightness": (wt.BOOL, [wt.HANDLE, ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD)]),
        "SetMonitorBrightness": (wt.BOOL, [wt.HANDLE, wt.DWORD]),
        "DestroyPhysicalMonitor": (wt.BOOL, [wt.HANDLE]),
    })

SPI_SETDESKWALLPAPER = 0x0014
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02

SESSION_DWORD = ctypes.c_uint32
WTSSessionInfoEx = 25
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


class WTSINFOEX_LEVEL1_W(ctypes.Structure):
    _fields_ = [
        ("SessionId", ctypes.c_uint32), ("SessionState", ctypes.c_int32),
        ("SessionFlags", ctypes.c_int32),
        ("WinStationName", ctypes.c_uint16 * 33),
        ("UserName", ctypes.c_uint16 * 21),
        ("DomainName", ctypes.c_uint16 * 18),
        ("LogonTime", ctypes.c_int64), ("ConnectTime", ctypes.c_int64),
        ("DisconnectTime", ctypes.c_int64), ("LastInputTime", ctypes.c_int64),
        ("CurrentTime", ctypes.c_int64),
        ("IncomingBytes", ctypes.c_uint32), ("OutgoingBytes", ctypes.c_uint32),
        ("IncomingFrames", ctypes.c_uint32), ("OutgoingFrames", ctypes.c_uint32),
        ("IncomingCompressedBytes", ctypes.c_uint32),
        ("OutgoingCompressedBytes", ctypes.c_uint32),
    ]


class WTSINFOEX_LEVEL_W(ctypes.Union):
    _fields_ = [("WTSInfoExLevel1", WTSINFOEX_LEVEL1_W)]


class WTSINFOEXW(ctypes.Structure):
    _fields_ = [("Level", ctypes.c_uint32), ("Data", WTSINFOEX_LEVEL_W)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_uint8), ("BatteryFlag", ctypes.c_uint8),
        ("BatteryLifePercent", ctypes.c_uint8), ("SystemStatusFlag", ctypes.c_uint8),
        ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32),
    ]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", ctypes.c_uint32), ("Luid", LUID), ("Attributes", ctypes.c_uint32)]


EWX_LOGOFF = 0
EWX_SHUTDOWN = 0x00000001
EWX_REBOOT = 0x00000002
EWX_FORCE = 0x00000004
EWX_POWEROFF = 0x00000008
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x2
SE_SHUTDOWN_NAME = "SeShutdownPrivilege"
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007
POWER_WNDPROC = _callback(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t)
POWER_MSG = wt.MSG


class POWER_WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint), ("lpfnWndProc", POWER_WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
    ]



@lru_cache(maxsize=1)
def wtsapi32():
    return _load("wtsapi32", {
        "WTSQuerySessionInformationW": (wt.BOOL, [ctypes.c_void_p, SESSION_DWORD, ctypes.c_int,
                                                  ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(SESSION_DWORD)]),
        "WTSFreeMemory": (None, [ctypes.c_void_p]),
    })


@lru_cache(maxsize=1)
def advapi32():
    return _load("advapi32", {
        "OpenProcessToken": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]),
        "LookupPrivilegeValueW": (ctypes.c_int, [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(LUID)]),
        "AdjustTokenPrivileges": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(TOKEN_PRIVILEGES),
                                                ctypes.c_uint32, ctypes.POINTER(TOKEN_PRIVILEGES), ctypes.POINTER(ctypes.c_uint32)]),
    })


@lru_cache(maxsize=1)
def powrprof():
    return _load("powrprof", {
        "SetSuspendState": (ctypes.c_ubyte, [ctypes.c_ubyte] * 3),
    })

WLAN_DWORD = ctypes.c_uint32
WLAN_GUID = ctypes.c_ubyte * 16
WLAN_CONNECTED = 1
WLAN_CURRENT_CONNECTION = 7
WLAN_INVALID_STATE = 5023
CREATE_NO_WINDOW = 0x08000000


class WlanInterfaceInfo(ctypes.Structure):
    _fields_ = [("guid", WLAN_GUID), ("description", ctypes.c_uint16 * 256), ("state", WLAN_DWORD)]


class WlanProfileInfo(ctypes.Structure):
    _fields_ = [("name", ctypes.c_uint16 * 256), ("flags", WLAN_DWORD)]


class WlanSSID(ctypes.Structure):
    _fields_ = [("length", WLAN_DWORD), ("data", ctypes.c_ubyte * 32)]


class WlanAssociation(ctypes.Structure):
    _fields_ = [
        ("ssid", WlanSSID), ("bss_type", WLAN_DWORD), ("bssid", ctypes.c_ubyte * 6),
        ("phy_type", WLAN_DWORD), ("phy_index", WLAN_DWORD), ("quality", WLAN_DWORD),
        ("rx_rate", WLAN_DWORD), ("tx_rate", WLAN_DWORD),
    ]


class WlanSecurity(ctypes.Structure):
    _fields_ = [
        ("enabled", WLAN_DWORD), ("one_x_enabled", WLAN_DWORD),
        ("auth_algorithm", WLAN_DWORD), ("cipher_algorithm", WLAN_DWORD),
    ]


class WlanConnectionInfo(ctypes.Structure):
    _fields_ = [
        ("state", WLAN_DWORD), ("mode", WLAN_DWORD), ("profile_name", ctypes.c_uint16 * 256),
        ("association", WlanAssociation), ("security", WlanSecurity),
    ]


@lru_cache(maxsize=1)
def wlanapi():
    pvoid = ctypes.c_void_p
    dword = WLAN_DWORD
    guid = ctypes.POINTER(WLAN_GUID)
    return _load("wlanapi", {
        "WlanOpenHandle": (dword, [dword, pvoid, ctypes.POINTER(dword), ctypes.POINTER(pvoid)]),
        "WlanCloseHandle": (dword, [pvoid, pvoid]),
        "WlanEnumInterfaces": (dword, [pvoid, pvoid, ctypes.POINTER(pvoid)]),
        "WlanQueryInterface": (dword, [pvoid, guid, dword, pvoid, ctypes.POINTER(dword),
                                      ctypes.POINTER(pvoid), ctypes.POINTER(dword)]),
        "WlanGetProfileList": (dword, [pvoid, guid, pvoid, ctypes.POINTER(pvoid)]),
        "WlanGetProfile": (dword, [pvoid, guid, ctypes.c_wchar_p, pvoid, ctypes.POINTER(pvoid),
                                  ctypes.POINTER(dword), ctypes.POINTER(dword)]),
        "WlanFreeMemory": (None, [pvoid]),
    })


@lru_cache(maxsize=1)
def iphlpapi():
    return _load("iphlpapi", {
        "ConvertInterfaceGuidToLuid": (WLAN_DWORD, [ctypes.POINTER(WLAN_GUID), ctypes.POINTER(ctypes.c_uint64)]),
        "ConvertInterfaceLuidToAlias": (WLAN_DWORD, [ctypes.POINTER(ctypes.c_uint64), ctypes.c_wchar_p, ctypes.c_size_t]),
    })
