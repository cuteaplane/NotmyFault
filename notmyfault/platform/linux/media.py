from __future__ import annotations

from notmyfault.platform._run import CommandRunner, default_runner, failed_detail
from notmyfault.platform.errors import BackendFailedError, BackendInvalidError, BackendMissingError
from .audio import AudioBackend

_COMMANDS = {
    "play_pause": ("play-pause",),
    "stop": ("stop",),
    "next": ("next",),
    "previous": ("previous",),
    "volume_up": ("volume", "0.05+"),
    "volume_down": ("volume", "0.05-"),
}


class MediaBackend:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def send(self, command: str) -> None:
        if command == "mute":
            AudioBackend(self._runner).set_muted(True)
            return
        if command not in _COMMANDS:
            raise BackendInvalidError(f"未知媒体命令: {command!r}")
        tool = self._runner.which("playerctl")
        if not tool:
            raise BackendMissingError("缺少媒体控制后端，请安装 playerctl")
        result = self._runner.run([tool, *_COMMANDS[command]], timeout=5)
        if result.returncode != 0:
            raise BackendFailedError(failed_detail(result))
