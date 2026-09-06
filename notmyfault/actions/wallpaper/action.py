import ctypes
import os
import subprocess
from pathlib import Path

from notmyfault.plugin_api import native_lock

SPI_SETDESKWALLPAPER = 0x0014
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02

_WALLPAPER_STYLES = {
    "fill": "10",
    "fit": "6",
    "stretch": "2",
    "tile": "0",
    "center": "0",
}


def run(action_info, params):
    image_path = params.get("image_path", "").strip()
    style = params.get("style", "fill")

    if not image_path:
        raise ValueError("未指定图片路径")
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"图片不存在: {image_path}")

    abspath = os.path.abspath(image_path)
    if not abspath.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
        raise ValueError(f"不支持的图片格式（仅支持 jpg/jpeg/png/bmp）: {abspath}")

    print(f"[Action:wallpaper] 设置壁纸: {abspath}")

    if os.name != "nt":
        from notmyfault.platform.linux_support import command_path, desktop_environment

        desktop = desktop_environment()
        if desktop == "gnome":
            uri = Path(abspath).as_uri()
            for key in ("picture-uri", "picture-uri-dark"):
                subprocess.run(
                    ["gsettings", "set", "org.gnome.desktop.background", key, uri],
                    check=True,
                    timeout=5,
                )
            style_map = {
                "fill": "zoom",
                "fit": "scaled",
                "stretch": "stretched",
                "tile": "wallpaper",
                "center": "centered",
            }
            subprocess.run(
                [
                    "gsettings",
                    "set",
                    "org.gnome.desktop.background",
                    "picture-options",
                    style_map.get(style, "zoom"),
                ],
                check=True,
                timeout=5,
            )
        elif command_path("plasma-apply-wallpaperimage"):
            subprocess.run(
                ["plasma-apply-wallpaperimage", abspath],
                check=True,
                timeout=10,
            )
        else:
            raise RuntimeError("未找到支持的 Linux 壁纸后端")
        print("[Action:wallpaper] 壁纸已更换")
        return

    with native_lock():
        user32 = ctypes.windll.user32
        user32.SystemParametersInfoW.argtypes = [
            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
        ]
        user32.SystemParametersInfoW.restype = ctypes.c_int
        result = user32.SystemParametersInfoW(
            SPI_SETDESKWALLPAPER, 0, ctypes.c_wchar_p(abspath),
            SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
        )
    if not result:
        raise RuntimeError(
            f"SystemParametersInfoW 设置壁纸失败（返回 {result}）"
        )

    # 壁纸已生效后再写样式，样式写入失败时壁纸仍保留
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Control Panel\Desktop",
                        0, winreg.KEY_SET_VALUE) as k:
        style_key = _WALLPAPER_STYLES.get(style, "10")
        winreg.SetValueEx(k, "WallpaperStyle", 0, winreg.REG_SZ, style_key)
        winreg.SetValueEx(k, "TileWallpaper", 0, winreg.REG_SZ,
                          "0" if style != "tile" else "1")
    print(f"[Action:wallpaper] 壁纸已更换")
