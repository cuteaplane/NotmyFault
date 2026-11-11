from __future__ import annotations

import os
import re
import subprocess

from notmyfault.platform._run import CommandRunner
from notmyfault.platform.errors import (
    BackendFailedError,
    BackendInvalidError,
    BackendMissingError,
)

_LANGUAGE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\+[A-Za-z][A-Za-z0-9_]*)*$"
)


def recognize(runner: CommandRunner, image_path: str, language: str) -> str:
    """用本机 tesseract 识别一张图片里的文字"""
    if not isinstance(image_path, str) or not image_path.strip():
        raise BackendInvalidError("缺少图片路径")
    if not isinstance(language, str) or not _LANGUAGE_RE.fullmatch(language):
        raise BackendInvalidError(f"无法识别的语言代码: {language!r}")
    executable = runner.which("tesseract")
    if not executable:
        raise BackendMissingError("依赖缺失：屏幕文字识别需要 tesseract")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    result = runner.run(
        [executable, image_path, "stdout", "-l", language],
        timeout=60,
        encoding="utf-8",
        creationflags=flags,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BackendFailedError(detail or f"tesseract 退出码 {result.returncode}")
    return (result.stdout or "").lstrip("\ufeff").strip()
