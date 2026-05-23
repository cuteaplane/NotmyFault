# Experimental feature !
import os
import sys
from pathlib import Path
from ctypes import POINTER, Structure, Union, byref, cast, create_unicode_buffer, c_wchar_p
from ctypes.wintypes import DWORD, WORD
import comtypes
import comtypes.client
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
from comtypes.persist import IPersistFile
from comtypes.shelllink import ShellLink, IShellLinkW
from comtypes.automation import VARTYPE, VT_LPWSTR


class PROPERTYKEY(Structure):
    _fields_ = [
        ("fmtid", GUID),
        ("pid", DWORD),
    ]


class PROPVARIANT_UNION(Union):
    _fields_ = [
        ("pwszVal", c_wchar_p),
        ("punkVal", POINTER(IUnknown)),
        ("pszVal", c_wchar_p),
        ("hVal", c_wchar_p),
    ]


class PROPVARIANT(Structure):
    _fields_ = [
        ("vt", VARTYPE),
        ("wReserved1", WORD),
        ("wReserved2", WORD),
        ("wReserved3", WORD),
        ("union", PROPVARIANT_UNION),
    ]


class IPropertyStore(IUnknown):
    _iid_ = GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (['out'], POINTER(DWORD), 'cProps')),
        COMMETHOD([], HRESULT, "GetAt", (['in'], DWORD, 'iProp'), (['out'], POINTER(PROPERTYKEY), 'pkey')),
        COMMETHOD([], HRESULT, "GetValue", (['in'], POINTER(PROPERTYKEY), 'key'), (['out'], POINTER(PROPVARIANT), 'pv')),
        COMMETHOD([], HRESULT, "SetValue", (['in'], POINTER(PROPERTYKEY), 'key'), (['in'], POINTER(PROPVARIANT), 'propvar')),
        COMMETHOD([], HRESULT, "Commit"),
    ]


def create_shortcut(lnk_path, target, args="", icon=None, aumid="cuteaplane.notmyfault.app"):
    comtypes.CoInitialize()
    try:
        # 创建 .lnk
        shell_link = comtypes.client.CreateObject(ShellLink, interface=IShellLinkW)
        shell_link.SetPath(str(target))
        if args:
            shell_link.SetArguments(args)
        if icon:
            shell_link.SetIconLocation(str(icon), 0)

        lnk_path.parent.mkdir(parents=True, exist_ok=True)
        persist_file = shell_link.QueryInterface(IPersistFile)
        persist_file.Save(str(lnk_path), 0)

        # 打开快捷方式并设置 PKEY_AppUserModel_ID
        property_store = shell_link.QueryInterface(IPropertyStore)
        pkey = PROPERTYKEY(
            fmtid=GUID("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"),
            pid=5,
        )
        buf = create_unicode_buffer(aumid)
        propvar = PROPVARIANT()
        propvar.vt = VT_LPWSTR
        propvar.union.pwszVal = cast(buf, c_wchar_p)
        property_store.SetValue(byref(pkey), byref(propvar))
        property_store.Commit()

        # 第二次保存时保持 propvar 和 buf 有效
        persist_file.Save(str(lnk_path), 1)
    finally:
        comtypes.CoUninitialize()

if __name__ == "__main__":
    start_menu = Path(os.getenv("APPDATA")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    start_menu.mkdir(parents=True, exist_ok=True)
    lnk = start_menu / "NotmyFault.lnk"
    root = Path(__file__).resolve().parents[1]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    script = root / "NOTMYFAULT.pyw"
    icon = root / "logo.ico"
    create_shortcut(
        lnk,
        exe,
        args=f'"{script}"',
        icon=icon,
        aumid="cuteaplane.notmyfault.app",
    )
    print(f"Shortcut created: {lnk}")
    print(f"Exists: {lnk.exists()}")