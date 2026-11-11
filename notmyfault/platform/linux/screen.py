from __future__ import annotations

from pathlib import Path

from notmyfault.platform import _ocr
from notmyfault.platform._run import CommandRunner, default_runner, failed_detail
from notmyfault.platform.errors import (
    BackendFailedError,
    BackendInvalidError,
    BackendMissingError,
    BackendUnsupportedError,
)
from notmyfault.platform.linux import desktop
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
        if fmt != "png":
            try:
                from PIL import Image
            except ImportError as error:
                raise BackendMissingError("保存 BMP/JPG 截图需要 Pillow") from error
        destination = Path(output_path or default_output_path("screenshot", fmt)).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        capture_path = destination
        if fmt != "png":
            capture_path = destination.with_suffix(".capture.png")

        try:
            if desktop.session_type() == "wayland":
                from notmyfault.platform.linux.portal import take_screenshot

                take_screenshot(
                    str(capture_path),
                    interactive=mode == "active_window",
                )
            else:
                command = self._x11_command(capture_path, mode)
                result = self._runner.run(command, timeout=30)
                if result.returncode != 0:
                    raise BackendFailedError(failed_detail(result))
            if capture_path != destination:
                with Image.open(capture_path) as image:
                    if fmt in ("jpg", "jpeg"):
                        image.convert("RGB").save(destination, "JPEG", quality=92)
                    else:
                        image.convert("RGB").save(destination, "BMP")
        finally:
            if capture_path != destination:
                capture_path.unlink(missing_ok=True)
        return str(destination)

    def _x11_command(self, capture_path: Path, mode: str) -> list[str]:
        if executable := self._runner.which("gnome-screenshot"):
            command = [executable, "-f", str(capture_path)]
            if mode == "active_window":
                command.insert(1, "-w")
            return command
        if executable := self._runner.which("spectacle"):
            return [
                executable,
                "-b",
                "-n",
                "-a" if mode == "active_window" else "-f",
                "-o",
                str(capture_path),
            ]
        if executable := self._runner.which("import"):
            if mode == "active_window":
                raise BackendUnsupportedError(
                    "ImageMagick import 后端不支持当前活动窗口截图，请安装 gnome-screenshot 或 spectacle"
                )
            return [executable, "-window", "root", str(capture_path)]
        raise BackendMissingError(
            "依赖缺失：屏幕截图需要 gnome-screenshot、spectacle 或 ImageMagick import"
        )

    def ocr(self, image_path: str, language: str) -> str:
        return _ocr.recognize(self._runner, image_path, language)
