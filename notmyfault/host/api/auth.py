from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Callable


def restrict_token_file(path: str) -> None:
    from notmyfault.security.api_key_store import _restrict_key_file

    _restrict_key_file(path)


class ApiTokenStore:
    def __init__(
        self,
        path: Path,
        token: str | None = None,
        writer: Callable[[str], None] | None = None,
        permission_restrictor: Callable[[str], None] = restrict_token_file,
    ) -> None:
        self.path = path
        self._writer = writer
        self._permission_restrictor = permission_restrictor
        self._token = token or self._load_or_create()

    @property
    def token(self) -> str:
        return self._token

    def matches(self, candidate: str) -> bool:
        return bool(candidate) and candidate.isascii() and secrets.compare_digest(candidate, self._token)

    def repair_file(self) -> None:
        try:
            if self.matches(self.path.read_text(encoding="utf-8").strip()):
                return
        except (OSError, UnicodeDecodeError):
            pass
        self._secure_write(self._token)

    def _load_or_create(self) -> str:
        try:
            token = self.path.read_text(encoding="utf-8").strip()
            if len(token) == 64 and token.isascii():
                int(token, 16)
                self._secure_write(token)
                return token
        except (OSError, ValueError):
            pass
        token = secrets.token_hex(32)
        self._secure_write(token)
        return token

    def _secure_write(self, token: str) -> None:
        if self._writer is not None:
            self._writer(token)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.parent / (
            f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            descriptor = os.open(
                str(tmp_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(descriptor)
            self._permission_restrictor(str(tmp_path))
            with open(tmp_path, "w", encoding="utf-8", newline="") as file:
                file.write(token)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self.path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
