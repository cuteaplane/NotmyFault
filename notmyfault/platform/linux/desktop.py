from __future__ import annotations

import os
from pathlib import Path
import shlex

from notmyfault.platform._run import default_runner, failed_detail
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError, BackendPermissionDeniedError

from notmyfault.platform.errors import BackendMissingError


_EDITING_PROCESS_NAMES = {
    "winword.exe", "excel.exe", "powerpnt.exe", "wps.exe", "et.exe", "wpp.exe",
    "acrobat.exe", "acrord32.exe", "libreoffice", "soffice.bin",
    "onlyoffice-desktopeditors", "wps", "et", "wpp",
}
_DOCUMENT_EXTENSIONS = {
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
    ".pdf",
}


def desktop_environment() -> str:
    value = ":".join(
        filter(
            None,
            (
                os.environ.get("XDG_CURRENT_DESKTOP", ""),
                os.environ.get("XDG_SESSION_DESKTOP", ""),
                os.environ.get("DESKTOP_SESSION", ""),
            ),
        )
    ).lower()
    if "gnome" in value or "ubuntu" in value:
        return "gnome"
    if "kde" in value or "plasma" in value:
        return "kde"
    return "unknown"


def session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() or (
        "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11"
    )


class DesktopBackend:
    def __init__(self, runner=None):
        self._runner = runner or default_runner

    def file_in_use(self, path: str) -> bool:
        try:
            with open(path, "rb"):
                pass
        except OSError:
            return True
        if Path(path).suffix.lower() not in _DOCUMENT_EXTENSIONS:
            return False
        try:
            import psutil
        except ImportError as error:
            raise BackendMissingError("缺少 psutil，无法检查文档进程") from error
        target = os.path.realpath(path)
        for process in psutil.process_iter(["pid", "name", "open_files"]):
            try:
                if (process.info["name"] or "").lower() not in _EDITING_PROCESS_NAMES:
                    continue
                if any(os.path.realpath(item.path) == target
                       for item in (process.info["open_files"] or [])):
                    return True
            except (psutil.Error, OSError):
                continue
        return False

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
        if desktop_environment() == "gnome":
            executable = self._runner.which("gsettings")
            if not executable:
                raise BackendMissingError("未安装 gsettings")
            keys = self._runner.run([executable, "list-keys", "org.gnome.desktop.background"], timeout=5)
            if keys.returncode:
                raise BackendFailedError(failed_detail(keys))
            commands = [[executable, "set", "org.gnome.desktop.background", key, Path(absolute).as_uri()]
                        for key in ("picture-uri", "picture-uri-dark")
                        if key == "picture-uri" or key in keys.stdout.splitlines()]
            commands.append([executable, "set", "org.gnome.desktop.background", "picture-options",
                             {"fill": "zoom", "fit": "scaled", "stretch": "stretched", "center": "centered", "tile": "wallpaper", "span": "spanned"}[style]])
        elif executable := self._runner.which("plasma-apply-wallpaperimage"):
            if style != "fill":
                raise BackendInvalidError("当前 Plasma 壁纸后端只支持填充样式")
            commands = [[executable, absolute]]
        else:
            raise BackendMissingError("未找到支持的 Linux 壁纸后端")
        for command in commands:
            result = self._runner.run(command, timeout=10)
            if result.returncode:
                raise BackendFailedError(failed_detail(result))

    def create_shortcut(self, target, location, name, arguments="", icon=""):
        name = self._shortcut_values(target, location, name, arguments, icon)
        try:
            argument_parts = shlex.split(arguments) if arguments else []
        except ValueError as error:
            raise BackendInvalidError("快捷方式参数引号不完整") from error
        def desktop_token(value: str) -> str:
            escaped = value.replace("\\", "\\\\\\\\")
            for char in ('"', "`", "$"):
                escaped = escaped.replace(char, "\\\\" + char)
            escaped = escaped.replace("%", "%%")
            return f'"{escaped}"'.replace("\t", "\\t")

        def desktop_value(value: str) -> str:
            return value.replace("\\", "\\\\").replace(" ", "\\s").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

        exec_line = " ".join(desktop_token(item) for item in (target, *argument_parts))
        desktop_entry = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={desktop_value(name)}\n"
            f"Exec={exec_line}\n"
            "Terminal=false\n"
        )
        if icon:
            desktop_entry += f"Icon={desktop_value(icon)}\n"
        if location == "desktop":
            base = Path.home() / "Desktop"
            if not base.is_dir():
                base = Path.home() / "桌面"
        elif location == "start_menu":
            base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart"
        path = base / (name.replace(" ", "_") + ".desktop")
        try:
            base.mkdir(parents=True, exist_ok=True)
            path.write_text(desktop_entry, encoding="utf-8")
            path.chmod(0o755)
        except PermissionError as error:
            raise BackendPermissionDeniedError(str(error)) from error
        except OSError as error:
            raise BackendFailedError(str(error)) from error
        return {"shortcut_path": str(path)}
