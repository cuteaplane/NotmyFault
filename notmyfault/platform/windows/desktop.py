import ctypes

from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform.windows import _win32


import os
from pathlib import Path

from notmyfault.platform._run import default_runner, powershell_path, failed_detail
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError, BackendPermissionDeniedError

_POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;


public static class NotmyFaultLink
{
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellLinkW
    {
        // 方法顺序对应 IShellLinkW 的 COM 槽位，未使用的方法也占据槽位
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path,
                     int length, IntPtr findData, uint flags);
        void GetIDList(out IntPtr itemList);
        void SetIDList(IntPtr itemList);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int length);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder arguments, int length);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string arguments);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int showCommand);
        void SetShowCmd(int showCommand);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }

    public static void Save(string path, string target, string arguments, string directory, string icon)
    {
        object instance = new ShellLink();
        try
        {
            IShellLinkW link = (IShellLinkW)instance;
            link.SetPath(target);
            if (!String.IsNullOrEmpty(arguments)) link.SetArguments(arguments);
            if (!String.IsNullOrEmpty(directory)) link.SetWorkingDirectory(directory);
            if (!String.IsNullOrEmpty(icon)) link.SetIconLocation(icon, 0);
            link.SetShowCmd(1);
            ((IPersistFile)instance).Save(path, true);
        }
        finally
        {
            Marshal.FinalReleaseComObject(instance);
        }
    }
}
'@
$name = $env:NMF_LNK_NAME
$target = $env:NMF_LNK_TARGET
if (-not $name -or -not $target) { throw "缺少快捷方式名称或目标路径" }
if ($env:NMF_LNK_LOCATION -eq 'start_menu') {
    $base = [Environment]::GetFolderPath('StartMenu')
} elseif ($env:NMF_LNK_LOCATION -eq 'startup') {
    $base = [Environment]::GetFolderPath('Startup')
} else {
    $base = [Environment]::GetFolderPath('Desktop')
}
$path = Join-Path $base ($name + '.lnk')
[NotmyFaultLink]::Save($path, $target, $env:NMF_LNK_ARGS, $env:NMF_LNK_WORKDIR, $env:NMF_LNK_ICON)
Write-Output $path
"""


class DesktopBackend:
    def __init__(self, runner=None):
        self._runner = runner or default_runner

    def file_in_use(self, path: str) -> bool:
        with NATIVE_LOCK:
            kernel32 = _win32.kernel32()
            handle = kernel32.CreateFileW(
                path, 0x80000000, 0, None, 3, 0x80, None,
            )
            if handle == ctypes.c_void_p(-1).value:
                return True
            try:
                return False
            finally:
                kernel32.CloseHandle(handle)

    def _wallpaper_path(self, path, style):
        if style not in ("fill", "fit", "stretch", "center", "tile", "span"):
            raise BackendInvalidError(f"未知壁纸样式: {style}")
        if not path or not Path(path).exists():
            raise BackendInvalidError(f"图片不存在: {path}")
        absolute = str(Path(path).absolute())
        if Path(absolute).suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp"):
            raise BackendInvalidError("不支持的图片格式，仅支持 jpg/jpeg/png/bmp")
        return absolute

    def _shortcut_values(self, target, location, name, arguments, icon):
        if location == "path":
            raise BackendInvalidError("location=path 需要输出目录参数，当前接口未提供")
        if location not in ("desktop", "start_menu", "startup"):
            raise BackendInvalidError(f"无效的创建位置: {location}")
        if not isinstance(name, str) or not name.strip() or ".." in name or any(ch in name for ch in '\\/:*?"<>|') or any(ord(ch) < 32 for ch in name):
            raise BackendInvalidError("快捷方式名称不合法")
        if not isinstance(target, str) or not target or any(ch in target for ch in ("\r", "\n")):
            raise BackendInvalidError("快捷方式目标路径为空或含换行")
        if any(ch in arguments for ch in ("\r", "\n")):
            raise BackendInvalidError("快捷方式参数不能换行")
        return name.strip()

    def set_wallpaper(self, path, style):
        absolute = self._wallpaper_path(path, style)
        import winreg
        try:
            with NATIVE_LOCK:
                result = _win32.user32().SystemParametersInfoW(
                    _win32.SPI_SETDESKWALLPAPER, 0, ctypes.c_wchar_p(absolute),
                    _win32.SPIF_UPDATEINIFILE | _win32.SPIF_SENDCHANGE,
                )
                if not result:
                    raise BackendFailedError("SystemParametersInfoW 设置壁纸失败")
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0, winreg.KEY_SET_VALUE) as key:
                    winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ,
                                      {"fill": "10", "fit": "6", "stretch": "2", "center": "0", "tile": "0", "span": "22"}[style])
                    winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, "1" if style == "tile" else "0")
        except PermissionError as error:
            raise BackendPermissionDeniedError(str(error)) from error
        except OSError as error:
            raise BackendFailedError(str(error)) from error

    def create_shortcut(self, target, location, name, arguments="", icon=""):
        name = self._shortcut_values(target, location, name, arguments, icon)
        env = os.environ.copy()
        env.update(NMF_LNK_LOCATION=location, NMF_LNK_NAME=name, NMF_LNK_TARGET=target,
                   NMF_LNK_ARGS=arguments, NMF_LNK_WORKDIR="", NMF_LNK_ICON=icon)
        result = self._runner.run(
            [powershell_path(), "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL_SCRIPT],
            timeout=30, env=env, encoding="utf-8", creationflags=0x08000000,
        )
        if result.returncode:
            raise BackendFailedError(f"创建快捷方式失败: {failed_detail(result)}")
        return {"shortcut_path": (result.stdout or "").strip()}
