"""屏幕文字识别：截取整个屏幕或其中一块区域，交给本机 tesseract 输出文字
Windows 用虚拟桌面 BitBlt；Linux 先走平台服务截全屏，再按区域裁剪
"""

from __future__ import annotations

import os
import re
import tempfile

from notmyfault.plugin_api import PlatformServiceError, native_lock, platform_services

NATIVE_LOCK = native_lock()

_MODES = frozenset({"fullscreen", "region"})
_LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\+[A-Za-z][A-Za-z0-9_]*)*$")
_DEFAULT_LANGUAGE = "chi_sim+eng"


def _require_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"{name} 必须是整数")
    return value


def _parse_language(raw) -> str:
    language = str(raw if raw is not None else _DEFAULT_LANGUAGE).strip()
    if not language:
        language = _DEFAULT_LANGUAGE
    if not _LANGUAGE_RE.fullmatch(language):
        raise ValueError(
            f"无法识别的语言代码: {language!r}（示例: chi_sim+eng 或 eng）"
        )
    return language


def _parse_region(params) -> tuple[int, int, int, int] | None:
    mode = str(params.get("mode") or "fullscreen").strip().lower()
    if mode not in _MODES:
        raise ValueError(f"未知识别范围: {mode!r}（可选: fullscreen/region）")
    if mode != "region":
        return None
    x = _require_int("距左边", params.get("x", 0))
    y = _require_int("距上边", params.get("y", 0))
    width = _require_int("宽度", params.get("width", 0))
    height = _require_int("高度", params.get("height", 0))
    if x < 0 or y < 0:
        raise ValueError("区域起点不能为负数")
    if width < 1 or height < 1:
        raise ValueError("宽度和高度至少为 1 像素")
    return x, y, width, height


def _ensure_region_inside(limit_width: int, limit_height: int, region, label: str) -> None:
    x, y, width, height = region
    if x + width > limit_width or y + height > limit_height:
        raise ValueError(
            f"识别区域超出{label}范围（{limit_width}×{limit_height}）"
        )


def _require_pillow():
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("裁剪或保存截图需要 Pillow") from None
    return Image


def _crop_png(source: str, destination: str, region) -> None:
    Image = _require_pillow()
    with Image.open(source) as image:
        _ensure_region_inside(image.width, image.height, region, "截图")
        x, y, width, height = region
        cropped = image.crop((x, y, x + width, y + height))
        cropped.save(destination, "PNG")


def _capture_linux(services, directory: str, region) -> str:
    status = services.capability("screen.capture")
    if not status.available:
        raise RuntimeError(status.reason or "无法截取屏幕")
    full_path = os.path.join(directory, "screen.png")
    try:
        services.capture_screen(full_path, "fullscreen", "png")
    except PlatformServiceError as error:
        raise RuntimeError(error.reason or "无法截取屏幕") from error
    if region is None:
        return full_path
    cropped = os.path.join(directory, "region.png")
    _crop_png(full_path, cropped, region)
    return cropped


def _capture_windows(destination: str, region) -> None:
    import ctypes
    import struct
    from ctypes import wintypes

    Image = _require_pillow()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
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
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int

    hdc_screen = None
    hdc_mem = None
    hbitmap = None
    previous_bitmap = None
    bitmap_selected = False
    try:
        with NATIVE_LOCK:
            virtual_left = user32.GetSystemMetrics(76)
            virtual_top = user32.GetSystemMetrics(77)
            virtual_width = user32.GetSystemMetrics(78)
            virtual_height = user32.GetSystemMetrics(79)
            if virtual_width <= 0 or virtual_height <= 0:
                raise RuntimeError(
                    f"无法获取屏幕尺寸（{virtual_width}x{virtual_height}），会话可能已锁定"
                )
            if region is None:
                src_x, src_y = virtual_left, virtual_top
                width, height = virtual_width, virtual_height
            else:
                _ensure_region_inside(virtual_width, virtual_height, region, "屏幕")
                x, y, width, height = region
                src_x = virtual_left + x
                src_y = virtual_top + y

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
            if not gdi32.BitBlt(
                hdc_mem, 0, 0, width, height, hdc_screen, src_x, src_y, 0x00CC0020
            ):
                raise RuntimeError("BitBlt 截图失败")
            if not gdi32.SelectObject(hdc_mem, previous_bitmap):
                raise RuntimeError("恢复 GDI 位图失败")
            bitmap_selected = False

            bmp_info = ctypes.create_string_buffer(40)
            # 正高度时 GetDIBits 返回 bottom-up
            struct.pack_into("<IiiHH", bmp_info, 0, 40, width, height, 1, 32)
            raw = ctypes.create_string_buffer(width * height * 4)
            if not gdi32.GetDIBits(hdc_screen, hbitmap, 0, height, raw, bmp_info, 0):
                raise RuntimeError("GetDIBits 读取像素失败")
            raw_bytes = bytes(raw)
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

    stride = width * 4
    top_down = b"".join(
        raw_bytes[row * stride:(row + 1) * stride]
        for row in range(height - 1, -1, -1)
    )
    Image.frombytes("RGB", (width, height), top_down, "raw", "BGRX", 0, 1).save(
        destination, "PNG"
    )


def _require_ocr(services) -> None:
    status = services.capability("screen.ocr")
    if not status.available:
        raise RuntimeError(status.reason or "屏幕文字识别不可用")


def run(action_info, params):
    region = _parse_region(params)
    language = _parse_language(params.get("language", _DEFAULT_LANGUAGE))
    services = platform_services()
    _require_ocr(services)
    mode = "region" if region is not None else "fullscreen"
    print(f"[Action:screen_ocr] 识别{mode}，语言 {language}")
    with tempfile.TemporaryDirectory(prefix="nmf-screen-ocr-") as directory:
        if os.name == "nt":
            image_path = os.path.join(directory, "screen.png")
            _capture_windows(image_path, region)
        else:
            image_path = _capture_linux(services, directory, region)
        try:
            text = services.recognize_image(image_path, language)
        except PlatformServiceError as error:
            raise RuntimeError(error.reason or "文字识别失败") from error
    print(f"[Action:screen_ocr] 识别完成，{len(text)} 个字符")
    return {"text": text, "language": language}
