from __future__ import annotations

import ctypes
import io
import struct
from pathlib import Path

from notmyfault.platform import _ocr
from notmyfault.platform._run import CommandRunner, default_runner
from notmyfault.platform._native import NATIVE_LOCK
from notmyfault.platform.errors import (
    BackendFailedError, BackendInvalidError, BackendMissingError,
)
from notmyfault.platform.windows import _win32
from notmyfault.platform.platform_support import default_output_path


class ScreenBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def capture(self, output_path: str, mode: str, fmt: str) -> str:
        if mode not in ("fullscreen", "active_window"):
            raise BackendInvalidError(f"不支持的截图范围: {mode}")
        if not isinstance(fmt, str) or fmt.lower() not in ("png", "jpg", "jpeg", "bmp"):
            raise BackendInvalidError("截图格式必须为 png、jpg、jpeg 或 bmp")
        fmt = fmt.lower()
        if fmt != "bmp":
            try:
                from PIL import Image
            except ImportError as error:
                raise BackendMissingError("保存 PNG/JPG 截图需要 Pillow") from error
        destination = Path(output_path or default_output_path("screenshot", fmt)).expanduser()
        user32 = _win32.user32()
        gdi32 = _win32.gdi32()
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
                        raise BackendFailedError("无法获取当前活动窗口")
                    rect = _win32.RECT()
                    if not user32.GetWindowRect(capture_hwnd, ctypes.byref(rect)):
                        raise BackendFailedError("无法获取当前活动窗口尺寸")
                    width = rect.right - rect.left
                    height = rect.bottom - rect.top
                else:
                    left = user32.GetSystemMetrics(76)
                    top = user32.GetSystemMetrics(77)
                    width = user32.GetSystemMetrics(78)
                    height = user32.GetSystemMetrics(79)
                if width <= 0 or height <= 0:
                    raise BackendFailedError(
                        f"无法获取屏幕尺寸（{width}x{height}），会话可能已锁定"
                    )
                hdc_screen = user32.GetDC(None)
                if not hdc_screen:
                    raise BackendFailedError("GetDC 失败，无法获取屏幕设备上下文")
                hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
                if not hdc_mem:
                    raise BackendFailedError("CreateCompatibleDC 失败")
                hbitmap = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
                if not hbitmap:
                    raise BackendFailedError("CreateCompatibleBitmap 失败")
                previous_bitmap = gdi32.SelectObject(hdc_mem, hbitmap)
                if not previous_bitmap or previous_bitmap == ctypes.c_void_p(-1).value:
                    raise BackendFailedError("SelectObject 失败")
                bitmap_selected = True
                if mode == "active_window":
                    if not user32.PrintWindow(capture_hwnd, hdc_mem, 0):
                        raise BackendFailedError("PrintWindow 截图失败")
                elif not gdi32.BitBlt(
                    hdc_mem, 0, 0, width, height, hdc_screen, left, top, 0x00CC0020
                ):
                    raise BackendFailedError("BitBlt 截图失败")
                if not gdi32.SelectObject(hdc_mem, previous_bitmap):
                    raise BackendFailedError("恢复 GDI 位图失败")
                bitmap_selected = False
                bmp_info = ctypes.create_string_buffer(40)
                # 正高度时 GetDIBits 返回 bottom-up 数据，BMP 文件按同样顺序写入。
                struct.pack_into("<IiiHH", bmp_info, 0, 40, width, height, 1, 32)
                bmp_bits = ctypes.create_string_buffer(width * height * 4)
                if not gdi32.GetDIBits(
                    hdc_screen, hbitmap, 0, height, bmp_bits, bmp_info, 0
                ):
                    raise BackendFailedError("GetDIBits 读取像素失败")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with io.BytesIO() as stream:
                row_size = (width * 3 + 3) & ~3
                pixel_size = row_size * height
                stream.write(b"BM")
                stream.write(struct.pack("<III", 54 + pixel_size, 0, 54))
                stream.write(struct.pack(
                    "<IiiHHIIiiII", 40, width, height, 1, 24, 0, pixel_size,
                    2835, 2835, 0, 0,
                ))
                # 32 位 DIB 像素顺序为 BGRA，BMP 24 位文件顺序为 BGR。
                pixels = bytes(bmp_bits)
                row_bgr = bytearray(row_size)
                for y in range(height):
                    start = y * width * 4
                    source = pixels[start:start + width * 4]
                    row_bgr[0:width * 3:3] = source[0:width * 4:4]
                    row_bgr[1:width * 3:3] = source[1:width * 4:4]
                    row_bgr[2:width * 3:3] = source[2:width * 4:4]
                    stream.write(row_bgr)
                if fmt == "bmp":
                    with destination.open("wb") as output:
                        output.write(stream.getbuffer())
                else:
                    stream.seek(0)
                    with Image.open(stream) as image:
                        if fmt in ("jpg", "jpeg"):
                            image.convert("RGB").save(destination, "JPEG", quality=92)
                        else:
                            image.save(destination, "PNG")
            return str(destination)
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

    def ocr(self, image_path: str, language: str) -> str:
        return _ocr.recognize(self._runner, image_path, language)
