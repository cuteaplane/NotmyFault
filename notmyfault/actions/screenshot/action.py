import os
import ctypes
import struct
import io
from datetime import datetime

from notmyfault.plugin_api import native_lock, platform_backend_api

NATIVE_LOCK = native_lock()
_platform_backend = platform_backend_api()
ScreenshotBackend = _platform_backend.ScreenshotBackend
default_runner = _platform_backend.default_runner

if os.name == "nt":
    from ctypes import wintypes

    with NATIVE_LOCK:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.BitBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC,
            wintypes.HBITMAP,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int


def run(action_info, params):
    mode = params.get("mode", "fullscreen")
    output_path = params.get("output_path", "").strip()
    fmt = str(params.get("format", "png")).lower()
    if fmt not in ("png", "jpg", "jpeg", "bmp"):
        raise ValueError("截图格式必须为 png、jpg、jpeg 或 bmp")
    if os.name == "nt" and fmt != "bmp":
        try:
            from PIL import Image
        except ImportError:
            raise RuntimeError("保存 PNG/JPG 截图需要 Pillow") from None

    if not output_path:
        if os.name == "nt":
            desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(desktop, f"screenshot_{ts}.{fmt}")
        else:
            from notmyfault.platform.linux_support import default_output_path
            output_path = str(default_output_path("screenshot", fmt))

    print(f"[Action:screenshot] 截取{mode} -> {output_path}")

    if os.name != "nt":
        result = ScreenshotBackend(default_runner).capture(output_path, mode, fmt)
        print(f"[Action:screenshot] 截图已保存: {result}")
        return {"file": str(result)}

    hdc_screen = None
    hdc_mem = None
    hbitmap = None
    previous_bitmap = None
    bitmap_selected = False
    try:
        with NATIVE_LOCK:
            capture_hwnd = None
            if mode == "active_window":
                capture_hwnd = user32.GetForegroundWindow()
                if not capture_hwnd:
                    raise RuntimeError("无法获取当前活动窗口")
                rect = wintypes.RECT()
                if not user32.GetWindowRect(capture_hwnd, ctypes.byref(rect)):
                    raise RuntimeError("无法获取当前活动窗口尺寸")
                width = rect.right - rect.left
                height = rect.bottom - rect.top
            else:
                left = user32.GetSystemMetrics(76)
                top = user32.GetSystemMetrics(77)
                width = user32.GetSystemMetrics(78)
                height = user32.GetSystemMetrics(79)
            if width <= 0 or height <= 0:
                raise RuntimeError(
                    f"无法获取屏幕尺寸（{width}x{height}），会话可能已锁定"
                )

            hdc_screen = user32.GetDC(None)
            if not hdc_screen:
                raise RuntimeError("GetDC 失败，无法获取屏幕设备上下文")
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                raise RuntimeError("CreateCompatibleDC 失败")
            hbitmap = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
            if not hbitmap:
                raise RuntimeError("CreateCompatibleBitmap 失败")
            previous_bitmap = gdi32.SelectObject(hdc_mem, hbitmap)
            if not previous_bitmap or previous_bitmap == ctypes.c_void_p(-1).value:
                raise RuntimeError("SelectObject 失败")
            bitmap_selected = True

            if mode == "active_window":
                if not user32.PrintWindow(capture_hwnd, hdc_mem, 0):
                    raise RuntimeError("PrintWindow 截图失败")
            else:
                if not gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, left, top, 0x00CC0020):
                    raise RuntimeError("BitBlt 截图失败")

            if not gdi32.SelectObject(hdc_mem, previous_bitmap):
                raise RuntimeError("恢复 GDI 位图失败")
            bitmap_selected = False

            bmp_info = ctypes.create_string_buffer(40)
            # 正高度时 GetDIBits 返回 bottom-up 数据，BMP 文件按同样顺序写入
            struct.pack_into("<IiiHH", bmp_info, 0, 40, width, height, 1, 32)

            bmp_size = width * height * 4
            bmp_bits = ctypes.create_string_buffer(bmp_size)
            if not gdi32.GetDIBits(hdc_screen, hbitmap, 0, height, bmp_bits, bmp_info, 0):
                raise RuntimeError("GetDIBits 读取像素失败")

        raw_bytes = bytes(bmp_bits)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with io.BytesIO() as f:
            row_size = (width * 3 + 3) & ~3
            pixel_size = row_size * height
            file_size = 54 + pixel_size
            f.write(b"BM")
            f.write(struct.pack("<I", file_size))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", 54))
            f.write(struct.pack("<I", 40))
            f.write(struct.pack("<i", width))
            f.write(struct.pack("<i", height))
            f.write(struct.pack("<H", 1))
            f.write(struct.pack("<H", 24))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", pixel_size))
            f.write(struct.pack("<I", 2835))
            f.write(struct.pack("<I", 2835))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", 0))
            # 32位 DIB 像素顺序为 BGRA，BMP 24位文件顺序为 BGR
            for y in range(height):
                start = y * width * 4
                src = raw_bytes[start:start + width * 4]
                row_bgr = bytearray(row_size)
                row_bgr[0:width * 3:3] = src[0:width * 4:4]      # B
                row_bgr[1:width * 3:3] = src[1:width * 4:4]      # G
                row_bgr[2:width * 3:3] = src[2:width * 4:4]      # R
                f.write(row_bgr)
            if fmt == "bmp":
                with open(output_path, "wb") as output:
                    output.write(f.getvalue())
            else:
                f.seek(0)
                with Image.open(f) as img:
                    if fmt in ("jpg", "jpeg"):
                        img.convert("RGB").save(output_path, "JPEG", quality=92)
                    else:
                        img.save(output_path, "PNG")

        print(f"[Action:screenshot] 截图已保存: {output_path}")
        return {"file": output_path}
    finally:
        with NATIVE_LOCK:
            if bitmap_selected and hdc_mem and previous_bitmap:
                try:
                    gdi32.SelectObject(hdc_mem, previous_bitmap)
                except Exception:
                    pass
            if hbitmap:
                try:
                    gdi32.DeleteObject(hbitmap)
                except Exception:
                    pass
            if hdc_mem:
                try:
                    gdi32.DeleteDC(hdc_mem)
                except Exception:
                    pass
            if hdc_screen:
                try:
                    user32.ReleaseDC(None, hdc_screen)
                except Exception:
                    pass
