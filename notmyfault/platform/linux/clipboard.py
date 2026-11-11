from __future__ import annotations

from pathlib import Path

from notmyfault.platform._run import CommandRunner, default_runner, failed_detail
from notmyfault.platform.errors import (
    BackendFailedError,
    BackendInvalidError,
    BackendMissingError,
)
from notmyfault.platform.linux import desktop


class ClipboardBackend:
    """wl-clipboard 或 xclip / xsel 剪贴板，Wayland 和 X11 都认"""

    _READ_ARGS = {
        "wl-paste": ["--no-newline", "--type", "text"],
        "xclip": ["-selection", "clipboard", "-o"],
    }
    _WRITE_ARGS = {
        "wl-copy": ["--type", "text/plain;charset=utf-8"],
        "xclip": ["-selection", "clipboard"],
        "xsel": ["--clipboard", "--input"],
    }

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or default_runner

    def _pick(self, write: bool) -> str | None:
        # wl-clipboard 依赖 Wayland 连接，X11 会话使用 xclip 或 xsel。
        if desktop.session_type() == "wayland":
            order = ("wl-copy",) if write else ("wl-paste",)
        else:
            order = ("xclip", "xsel")
        for name in order:
            if path := self._runner.which(name):
                return path
        # 会话类型拿不准时按装了哪个算哪个
        if desktop.session_type() not in ("wayland", "x11"):
            for name in self._WRITE_ARGS if write else self._READ_ARGS:
                if path := self._runner.which(name):
                    return path
        return None

    def _read_command(self, path: str) -> list[str] | None:
        name = Path(path).name
        if name == "xsel":
            return [path, "--clipboard", "--output", "--verbose", "--selectionTimeout", "2500"]
        query = (
            [path, "--list-types"] if name == "wl-paste"
            else [path, "-selection", "clipboard", "-o", "-target", "TARGETS"]
        )
        result = self._runner.run(query, timeout=3)
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            no_selection = (
                detail == "Nothing is copied" if name == "wl-paste"
                else detail == "Error: target TARGETS not available"
            )
            if no_selection:
                return None
            raise BackendFailedError(failed_detail(result))
        targets = result.stdout.splitlines()
        if name == "wl-paste":
            has_text = any(
                target.startswith("text/") or target in {"UTF8_STRING", "STRING", "TEXT"}
                for target in targets
            )
            return [path, *self._READ_ARGS[name]] if has_text else None
        target = next(
            (target for target in ("UTF8_STRING", "text/plain;charset=utf-8", "text/plain", "STRING", "TEXT")
             if target in targets),
            None,
        )
        if target is None:
            return None
        return [path, *self._READ_ARGS[name], "-target", target]

    def read(self, max_chars: int | None = None) -> str | None:
        if max_chars is not None and (
            isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0
        ):
            raise BackendInvalidError("max_chars 必须是正整数")
        path = self._pick(write=False)
        if not path:
            raise BackendMissingError(
                "依赖缺失：读取剪贴板需要 wl-clipboard（Wayland）或 xclip/xsel（X11）"
            )
        command = self._read_command(path)
        if command is None:
            return None
        if Path(path).name == "xsel":
            # xsel 的退出码不区分空文本和转换被拒绝，诊断信息写在 stderr。
            result = self._runner.run(command, timeout=3)
            detail = result.stderr or ""
            if result.returncode != 0 or "selection timed out" in detail:
                raise BackendFailedError(failed_detail(result))
            refusals = detail.count("Conversion refused") + detail.count("is not a string.")
            if refusals >= 2:
                return None
        elif max_chars is None:
            result = self._runner.run(command, timeout=3)
        else:
            result = self._runner.read_output(command, max_chars=max_chars, timeout=3)
        if result.returncode != 0:
            raise BackendFailedError(failed_detail(result))
        return result.stdout if max_chars is None else result.stdout[:max_chars]

    def write(self, text: str) -> None:
        if not isinstance(text, str):
            raise BackendInvalidError("剪贴板内容必须是文本")
        path = self._pick(write=True)
        if not path:
            raise BackendMissingError(
                "依赖缺失：写入剪贴板需要 wl-clipboard（Wayland）或 xclip/xsel（X11）"
            )
        args = self._WRITE_ARGS[Path(path).name]
        if Path(path).name == "xsel" and not text:
            args = [*args, "--follow"]
        result = self._runner.run([path, *args], timeout=3, input=text)
        if result.returncode != 0:
            raise BackendFailedError(failed_detail(result))

    def clear(self) -> None:
        path = self._pick(write=True)
        if not path:
            raise BackendMissingError(
                "依赖缺失：清空剪贴板需要 wl-clipboard（Wayland）或 xclip/xsel（X11）"
            )
        name = Path(path).name
        if name == "wl-copy":
            commands = [([path, "--clear"], None)]
        elif name == "xsel":
            commands = [([path, "--clipboard", "--clear"], None)]
        else:
            # xclip 在最后一次选择请求完成后释放剪贴板所有权。
            commands = [
                ([path, "-selection", "clipboard", "-loops", "1"], ""),
                ([path, "-selection", "clipboard", "-o", "-target", "TARGETS"], None),
            ]
        for command, text in commands:
            result = self._runner.run(command, timeout=3, input=text)
            if result.returncode != 0:
                raise BackendFailedError(failed_detail(result))
