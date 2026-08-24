from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Callable


def restrict_token_file(path: str) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return

    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1
    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    advapi32.SetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
    )
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)

    class SidAndAttributes(ctypes.Structure):
        _fields_ = (("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD))

    class TokenUser(ctypes.Structure):
        _fields_ = (("user", SidAndAttributes),)

    process_token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    security_descriptor = wintypes.LPVOID()
    try:
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(process_token)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            process_token,
            token_user_class,
            None,
            0,
            ctypes.byref(required),
        )
        if not required.value:
            raise ctypes.WinError(ctypes.get_last_error())
        token_buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            process_token,
            token_user_class,
            token_buffer,
            required,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.cast(token_buffer, ctypes.POINTER(TokenUser)).contents
        if not advapi32.ConvertSidToStringSidW(
            user.user.sid, ctypes.byref(sid_text)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = f"D:P(A;;FA;;;{sid_text.value})"
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            1,
            ctypes.byref(security_descriptor),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        security_information = (
            dacl_security_information | protected_dacl_security_information
        )
        if not advapi32.SetFileSecurityW(
            path,
            security_information,
            security_descriptor,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError as error:
        raise RuntimeError("无法设置 API 令牌文件权限") from error
    finally:
        if security_descriptor:
            kernel32.LocalFree(security_descriptor)
        if sid_text:
            kernel32.LocalFree(sid_text)
        if process_token:
            kernel32.CloseHandle(process_token)


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
        return bool(candidate) and secrets.compare_digest(candidate, self._token)

    def repair_file(self) -> None:
        try:
            if secrets.compare_digest(
                self.path.read_text(encoding="utf-8").strip(),
                self._token,
            ):
                return
        except OSError:
            pass
        self._secure_write(self._token)

    def _load_or_create(self) -> str:
        try:
            token = self.path.read_text(encoding="utf-8").strip()
            if len(token) == 64:
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
