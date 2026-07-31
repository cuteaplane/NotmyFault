import os
import ctypes
import subprocess
from pathlib import Path
from datetime import datetime

if os.name == "nt":
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32


def run(action_info, params):
    mode = params.get("mode", "fullscreen")
    output_path = params.get("output_path", "").strip()
    fmt = params.get("format", "png")

    if not output_path:
        if os.name == "nt":
            desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(desktop, f"screenshot_{ts}.{fmt}")
        else:
            from notmyfault.linux_support import default_output_path
            output_path = str(default_output_path("screenshot", fmt))

    print(f"[Action:screenshot] 截取{mode} -> {output_path}")

    if os.name != "nt":
        from notmyfault.linux_support import command_path, session_type

        destination = Path(output_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        capture_path = destination
        if fmt.lower() in ("jpg", "jpeg"):
            capture_path = destination.with_suffix(".capture.png")

        if session_type() == "wayland":
            from notmyfault.portal_screenshot import take_screenshot

            take_screenshot(
                str(capture_path),
                interactive=mode == "active_window",
            )
            command = None
        elif executable := command_path("gnome-screenshot"):
            command = [executable, "-f", str(capture_path)]
            if mode == "active_window":
                command.insert(1, "-w")
        elif session_type() == "wayland" and (executable := command_path("grim")):
            if mode == "active_window":
                raise RuntimeError("grim 无法安全识别 GNOME 当前窗口，请安装 gnome-screenshot")
            command = [executable, str(capture_path)]
        elif executable := command_path("spectacle"):
            command = [
                executable,
                "-b",
                "-n",
                "-a" if mode == "active_window" else "-f",
                "-o",
                str(capture_path),
            ]
        elif executable := command_path("import"):
            command = [executable, "-window", "root", str(capture_path)]
        else:
            raise RuntimeError("缺少截图后端，请安装 gnome-screenshot")

        if command:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "截图命令失败")

        if capture_path != destination:
            from PIL import Image
            with Image.open(capture_path) as image:
                image.convert("RGB").save(destination, "JPEG", quality=92)
            capture_path.unlink(missing_ok=True)
        print(f"[Action:screenshot] 截图已保存: {destination}")
        return str(destination)

    hdc_screen = None
    hdc_mem = None
    hbitmap = None
    try:
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
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
        gdi32.SelectObject(hdc_mem, hbitmap)

        if mode == "active_window":
            hwnd = user32.GetForegroundWindow()
            user32.PrintWindow(hwnd, hdc_mem, 0)
        else:
            if not gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, 0, 0, 0x00CC0020):
                raise RuntimeError("BitBlt 截图失败")

        bmp_info = ctypes.create_string_buffer(40)
        ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_uint32))[0] = 40
        ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_int32))[4] = width
        # 正高度 -> GetDIBits 返回 bottom-up 数据，与 BMP 文件正序写入一致
        ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_int32))[8] = height
        ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_uint16))[12] = 1
        ctypes.cast(bmp_info, ctypes.POINTER(ctypes.c_uint16))[14] = 32

        bmp_size = width * height * 4
        bmp_bits = ctypes.create_string_buffer(bmp_size)
        if not gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, bmp_bits, bmp_info, 0):
            raise RuntimeError("GetDIBits 读取像素失败")

        raw_bytes = bytes(bmp_bits)
        import struct
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
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
            # 32位 DIB 像素顺序为 BGRA，BMP 24位文件顺序为 BGR。
            # 用字节切片交错重组（C 级实现），比逐像素循环快一个数量级。
            for y in range(height):
                start = y * width * 4
                src = raw_bytes[start:start + width * 4]
                row_bgr = bytearray(row_size)
                row_bgr[0:width * 3:3] = src[0:width * 4:4]      # B
                row_bgr[1:width * 3:3] = src[1:width * 4:4]      # G
                row_bgr[2:width * 3:3] = src[2:width * 4:4]      # R
                f.write(row_bgr)

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
        return output_path
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
