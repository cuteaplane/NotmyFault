import ctypes
import os

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
        print("[Action:wallpaper] 未指定图片路径")
        return

    if not os.path.exists(image_path):
        print(f"[Action:wallpaper] 图片不存在: {image_path}")
        return

    abspath = os.path.abspath(image_path)
    if not abspath.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
        print(f"[Action:wallpaper] 不支持的图片格式，尝试设置: {abspath}")

    print(f"[Action:wallpaper] 设置壁纸: {abspath}")

    try:
        user32 = ctypes.windll.user32
        result = user32.SystemParametersInfoW(
            SPI_SETDESKWALLPAPER, 0, abspath,
            SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
        )
        if result:
            print(f"[Action:wallpaper] 壁纸已更换")
        else:
            print(f"[Action:wallpaper] SystemParametersInfoW 返回 {result}")

        style_key = _WALLPAPER_STYLES.get(style, "10")
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Control Panel\Desktop",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "WallpaperStyle", 0, winreg.REG_SZ, style_key)
            tile_val = "0" if style != "tile" else "1"
            winreg.SetValueEx(k, "TileWallpaper", 0, winreg.REG_SZ, tile_val)

    except Exception as e:
        print(f"[Action:wallpaper] 设置壁纸失败: {e}")
