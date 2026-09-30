"""屏幕找图：在屏幕或指定区域里查找与模板像素完全一致的位置
Windows 用虚拟桌面 BitBlt；Linux 先走平台服务截全屏，再按区域裁剪
匹配为 RGB 逐像素精确比对，缩放或抗锯齿会导致找不到
"""

from __future__ import annotations

import os
import tempfile
import time

from notmyfault.plugin_api import PlatformServiceError, native_lock, platform_services

NATIVE_LOCK = native_lock()

_MODES = frozenset({"fullscreen", "region"})
_IF_NOT_FOUND = frozenset({"error", "empty"})
_TEMPLATE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp")
_MAX_TIMEOUT = 86400.0
_MIN_INTERVAL = 0.1
_MAX_INTERVAL = 60.0


def _require_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"{name} 必须是整数")
    return value


def _require_number(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    number = float(value)
    if number != number:  # NaN
        raise ValueError(f"{name} 必须是数字")
    return number


def _parse_region(params) -> tuple[int, int, int, int] | None:
    mode = str(params.get("mode") or "fullscreen").strip().lower()
    if mode not in _MODES:
        raise ValueError(f"未知查找范围: {mode!r}（可选: fullscreen/region）")
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


def _parse_timeout(params) -> float:
    timeout = _require_number("查找时限", params.get("timeout_seconds", 0))
    if timeout < 0:
        raise ValueError("查找时限不能为负数")
    if timeout > _MAX_TIMEOUT:
        raise ValueError(f"查找时限不能超过 {_MAX_TIMEOUT:g} 秒")
    return timeout


def _parse_interval(params) -> float:
    interval = _require_number("重试间隔", params.get("interval_seconds", 0.5))
    if interval < _MIN_INTERVAL or interval > _MAX_INTERVAL:
        raise ValueError(
            f"重试间隔须在 {_MIN_INTERVAL:g}～{_MAX_INTERVAL:g} 秒之间"
        )
    return interval


def _parse_if_not_found(params) -> str:
    value = str(params.get("if_not_found") or "error").strip().lower()
    if value not in _IF_NOT_FOUND:
        raise ValueError("找不到时只能选择报错或返回未找到")
    return value


def _parse_template_path(params) -> str:
    raw = params.get("template_path", "")
    if not isinstance(raw, str):
        raise ValueError("模板图片路径必须是文本")
    path = raw.strip()
    if not path:
        raise ValueError("请填写模板图片路径")
    lower = path.lower()
    if not lower.endswith(_TEMPLATE_SUFFIXES):
        raise ValueError("模板图片须为 png、jpg、jpeg 或 bmp")
    if not os.path.isfile(path):
        raise ValueError(f"模板图片不存在: {path}")
    if not os.access(path, os.R_OK):
        raise ValueError(f"模板图片不可读: {path}")
    return path


def _ensure_region_inside(limit_width: int, limit_height: int, region, label: str) -> None:
    x, y, width, height = region
    if x + width > limit_width or y + height > limit_height:
        raise ValueError(
            f"查找区域超出{label}范围（{limit_width}×{limit_height}）"
        )


def _require_pillow():
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("找图需要 Pillow") from None
    return Image


def _crop_png(source: str, destination: str, region) -> None:
    Image = _require_pillow()
    with Image.open(source) as image:
        _ensure_region_inside(image.width, image.height, region, "截图")
        x, y, width, height = region
        cropped = image.crop((x, y, x + width, y + height))
        cropped.save(destination, "PNG")


def _capture_linux(services, directory: str, region) -> tuple[str, int, int]:
    status = services.capability("screen.capture")
    if not status.available:
        raise RuntimeError(status.reason or "无法截取屏幕")
    full_path = os.path.join(directory, "screen.png")
    try:
        services.capture_screen(full_path, "fullscreen", "png")
    except PlatformServiceError as error:
        raise RuntimeError(error.reason or "无法截取屏幕") from error
    # 与 screenshot / screen_ocr 一致：全屏图左上角按屏幕 (0, 0) 理解
    if region is None:
        return full_path, 0, 0
    cropped = os.path.join(directory, "region.png")
    _crop_png(full_path, cropped, region)
    return cropped, region[0], region[1]


def _capture_windows(destination: str, region) -> tuple[int, int]:
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
    origin_x = 0
    origin_y = 0
    width = 0
    height = 0
    raw_bytes = b""
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
                origin_x, origin_y = virtual_left, virtual_top
            else:
                _ensure_region_inside(virtual_width, virtual_height, region, "屏幕")
                x, y, width, height = region
                src_x = virtual_left + x
                src_y = virtual_top + y
                origin_x = virtual_left + x
                origin_y = virtual_top + y

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
    return origin_x, origin_y


def _load_rgb(path: str):
    Image = _require_pillow()
    with Image.open(path) as image:
        return image.convert("RGB")


def _find_exact(haystack, needle) -> tuple[int, int] | None:
    """在 haystack 中找与 needle RGB 完全一致的左上角；按行字节比对。"""
    hw, hh = haystack.size
    nw, nh = needle.size
    if nw > hw or nh > hh:
        raise ValueError(
            f"模板尺寸（{nw}×{nh}）大于查找区域（{hw}×{hh}）"
        )
    hay_bytes = haystack.tobytes()
    needle_bytes = needle.tobytes()
    hay_stride = hw * 3
    needle_stride = nw * 3
    first_row = needle_bytes[:needle_stride]
    max_y = hh - nh
    max_x = hw - nw
    for y in range(max_y + 1):
        row_base = y * hay_stride
        for x in range(max_x + 1):
            start = row_base + x * 3
            if hay_bytes[start:start + needle_stride] != first_row:
                continue
            matched = True
            for row in range(1, nh):
                h_start = (y + row) * hay_stride + x * 3
                n_start = row * needle_stride
                if (
                    hay_bytes[h_start:h_start + needle_stride]
                    != needle_bytes[n_start:n_start + needle_stride]
                ):
                    matched = False
                    break
            if matched:
                return x, y
    return None


def _capture_once(services, directory: str, region) -> tuple[str, int, int]:
    if os.name == "nt":
        image_path = os.path.join(directory, "screen.png")
        origin_x, origin_y = _capture_windows(image_path, region)
        return image_path, origin_x, origin_y
    return _capture_linux(services, directory, region)


def _match_once(services, directory: str, region, template) -> dict | None:
    image_path, origin_x, origin_y = _capture_once(services, directory, region)
    haystack = _load_rgb(image_path)
    match = _find_exact(haystack, template)
    if match is None:
        return None
    mx, my = match
    tw, th = template.size
    screen_x = origin_x + mx
    screen_y = origin_y + my
    return {
        "found": True,
        "x": screen_x,
        "y": screen_y,
        "width": tw,
        "height": th,
        "center_x": screen_x + tw // 2,
        "center_y": screen_y + th // 2,
    }


def _sleep_interval(seconds: float, cancellation) -> None:
    if seconds <= 0:
        return
    if cancellation is None:
        time.sleep(seconds)
        return
    if cancellation.wait(seconds):
        cancellation.raise_if_cancelled()


def _not_found_result(if_not_found: str) -> dict:
    if if_not_found == "empty":
        return {"found": False}
    raise RuntimeError("未在屏幕中找到与模板像素一致的图像")


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    template_path = _parse_template_path(params)
    region = _parse_region(params)
    timeout = _parse_timeout(params)
    interval = _parse_interval(params)
    if_not_found = _parse_if_not_found(params)
    runtime = context.get("runtime", {}) if isinstance(context, dict) else {}
    cancellation = runtime.get("cancellation") if isinstance(runtime, dict) else None
    services = platform_services()
    template = _load_rgb(template_path)
    mode = "region" if region is not None else "fullscreen"
    print(
        f"[Action:find_image] 查找{mode}，模板 {template.size[0]}×{template.size[1]}，"
        f"时限 {timeout:g}s"
    )
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="nmf-find-image-") as directory:
        while True:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            result = _match_once(services, directory, region, template)
            if result is not None:
                print(
                    f"[Action:find_image] 找到 ({result['x']}, {result['y']}) "
                    f"{result['width']}×{result['height']}"
                )
                return result
            if timeout <= 0:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _sleep_interval(min(interval, remaining), cancellation)
    return _not_found_result(if_not_found)
