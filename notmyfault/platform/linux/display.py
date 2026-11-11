from __future__ import annotations

from notmyfault.platform._run import CommandRunner, default_runner, failed_detail
from notmyfault.platform.errors import (
    BackendFailedError,
    BackendInvalidError,
    BackendMissingError,
    BackendUnsupportedError,
)
from notmyfault.platform.linux import desktop


class DisplayBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def set_brightness(self, percent: int) -> dict:
        if type(percent) is not int or not 0 <= percent <= 100:
            raise BackendInvalidError("亮度必须是 0–100 的整数")
        brightnessctl = self._runner.which("brightnessctl")
        if not brightnessctl:
            raise BackendMissingError("依赖缺失：亮度控制需要 brightnessctl")
        result = self._runner.run(
            [brightnessctl, "set", f"{percent}%"],
            timeout=5,
        )
        if result.returncode != 0:
            raise BackendFailedError(failed_detail(result))
        values = []
        for operation in ("get", "max"):
            result = self._runner.run([brightnessctl, operation], timeout=5)
            if result.returncode != 0:
                raise BackendFailedError(failed_detail(result))
            try:
                values.append(int(result.stdout.strip()))
            except (ValueError, AttributeError) as error:
                raise BackendFailedError(
                    f"无法解析 brightnessctl {operation} 回读值: {result.stdout!r}"
                ) from error
        current, maximum = values
        if maximum <= 0 or not 0 <= current <= maximum:
            raise BackendFailedError(f"亮度回读值无效: {current}/{maximum}")
        actual = current * 100 / maximum
        # 硬件亮度档位较少时，百分比只能取最接近的一档。
        tolerance = max(1.0, 50 / maximum)
        if abs(actual - percent) > tolerance:
            raise BackendFailedError(
                f"亮度设置为 {percent}%，回读为 {actual:.2f}%"
            )
        warnings = []
        if abs(actual - percent) > 0.01:
            warnings.append(f"硬件亮度量化：请求 {percent}%，回读 {actual:.2f}%")
        return {"brightness": percent, "methods": ["brightnessctl"], "warnings": warnings}

    def set_power(self, action: str) -> dict:
        if action not in ("on", "off"):
            raise BackendInvalidError(f"不支持的显示器电源操作: {action}")

        desktop_name = desktop.desktop_environment()
        session = desktop.session_type()
        active = "true" if action == "off" else "false"
        errors: list[str] = []

        def run_screensaver(dest: str, object_path: str, method: str) -> bool:
            executable = self._runner.which("gdbus")
            if not executable:
                errors.append("未安装 gdbus")
                return False
            result = self._runner.run(
                [
                    executable,
                    "call",
                    "--session",
                    "--dest",
                    dest,
                    "--object-path",
                    object_path,
                    "--method",
                    method,
                    active,
                ],
                timeout=5,
            )
            if result.returncode == 0:
                return True
            errors.append(f"{dest}: {failed_detail(result)}")
            return False

        def run_xset() -> bool:
            executable = self._runner.which("xset")
            if not executable:
                errors.append("未安装 xset")
                return False
            result = self._runner.run(
                [executable, "dpms", "force", action],
                timeout=5,
            )
            if result.returncode == 0:
                return True
            errors.append(f"xset: {failed_detail(result)}")
            return False

        if desktop_name == "gnome":
            if run_screensaver(
                "org.gnome.ScreenSaver",
                "/org/gnome/ScreenSaver",
                "org.gnome.ScreenSaver.SetActive",
            ):
                return {"methods": ["org.gnome.ScreenSaver"], "warnings": errors}
            raise BackendFailedError(
                "；".join(errors) or "GNOME 显示器开关失败"
            )

        if desktop_name == "kde":
            for dest, object_path in (
                ("org.kde.screensaver", "/ScreenSaver"),
                ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"),
            ):
                if run_screensaver(
                    dest,
                    object_path,
                    "org.freedesktop.ScreenSaver.SetActive",
                ):
                    return {"methods": [dest], "warnings": errors}
            if session != "wayland" and run_xset():
                return {"methods": ["xset"], "warnings": errors}
            raise BackendFailedError(
                "；".join(errors)
                or "KDE 显示器开关失败，需要可用的 ScreenSaver 接口或 xset"
            )

        if session != "wayland" and run_xset():
            return {"methods": ["xset"], "warnings": errors}

        if session == "wayland":
            raise BackendUnsupportedError(
                f"当前桌面（{desktop_name}）在 Wayland 下没有可用的显示器开关接口；"
                "GNOME 走 org.gnome.ScreenSaver，KDE 走 org.kde.screensaver，"
                "其他桌面请使用 X11 会话上的 xset"
            )
        raise BackendMissingError(
            "；".join(errors) or "依赖缺失：显示器开关需要 xset"
        )
