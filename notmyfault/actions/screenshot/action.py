import os
import ctypes
from datetime import datetime

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def run(action_info, params):
    mode = params.get("mode", "fullscreen")
    output_path = params.get("output_path", "").strip()
    fmt = params.get("format", "png")

    if not output_path:
        desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(desktop, f"screenshot_{ts}.{fmt}")

    print(f"[Action:screenshot] 截取{mode} -> {output_path}")

    hdc_screen = None
    hdc_mem = None
    hbitmap = None
    try:
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)

        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbitmap = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        gdi32.SelectObject(hdc_mem, hbitmap)

        if mode == "active_window":
            hwnd = user32.GetForegroundWindow()
            user32.PrintWindow(hwnd, hdc_mem, 0)
        else:
            gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, 0, 0, 0x00CC0020)

        bmp_info = ctypes.create_string_buffer(40)
        ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_uint32))[0] = 40
        ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_int32))[4] = width
        # 正高度 -> GetDIBits 返回 bottom-up 数据，与 BMP 文件正序写入一致
        ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_int32))[8] = height
        ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_uint16))[12] = 1
        ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_uint16))[14] = 32

        bmp_size = width * height * 4
        bmp_bits = ctypes.create_string_buffer(bmp_size)
        gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, bmp_bits, bmp_info, 0)

        raw_bytes = bytes(bmp_bits)
        import struct
        with open(output_path, "wb") as f:
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
                row_start = y * width * 4
                row = bytearray()
                for x in range(width):
                    offset = row_start + x * 4
                    row.extend([raw_bytes[offset], raw_bytes[offset + 1], raw_bytes[offset + 2]])
                row.extend(b"\x00" * (row_size - width * 3))
                f.write(bytes(row))

        if fmt == "jpg":
            try:
                from PIL import Image
                img = Image.open(output_path)
                jpg_path = output_path.replace(".png", ".jpg").replace(".bmp", ".jpg")
                img.convert("RGB").save(jpg_path, "JPEG", quality=92)
                if jpg_path != output_path:
                    os.remove(output_path)
                    output_path = jpg_path
            except ImportError:
                print("[Action:screenshot] JPG 转换需要 Pillow 库，已保存为 BMP")

        print(f"[Action:screenshot] 截图已保存: {output_path}")

    except Exception as e:
        print(f"[Action:screenshot] 截图失败: {e}")
    finally:
        # 确保 GDI 对象在任何路径下都被释放，避免泄漏
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
