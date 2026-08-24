"""AI API 密钥持久化，Windows 用 DPAPI，Linux 用 Secret Service。"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
_MAX_KEY_LENGTH = 4096
# 解密要拿同样的熵，所以熵写死成常量。
_ENTROPY = b"NotmyFault-AI-API-Key"
_UI_FORBIDDEN = 0x01
_CRYPT32 = None


class KeyStoreError(Exception):
    """密钥存储操作的基类异常。"""


class KeyStoreUnsupportedError(KeyStoreError):
    """当前平台不支持 DPAPI 密钥持久化。"""


class KeyStoreDecryptError(KeyStoreError):
    """密文损坏或无法解密。"""


class KeyStoreInvalidKeyError(KeyStoreError, ValueError):
    """密钥为空或超长。"""


class KeyStoreStatus(Enum):
    UNSUPPORTED = "unsupported"
    ABSENT = "absent"
    STORED = "stored"
    CORRUPT = "corrupt"


_SECRET_SERVICE_LABEL = "NotmyFault AI API Key"
_SECRET_SERVICE_ATTRIBUTES = {
    "application": "NotmyFault",
    "purpose": "ai_api_key",
}


def _is_windows() -> bool:
    return os.name == "nt"


def _supports_persistence() -> bool:
    if _is_windows():
        return True
    try:
        with _secret_service_collection():
            pass
    except KeyStoreError:
        return False
    return True


def _load_crypt32():
    global _CRYPT32
    if _CRYPT32 is not None:
        return _CRYPT32

    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte)))

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    _CRYPT32 = (crypt32, kernel32, DataBlob)
    return _CRYPT32


def _protect_bytes(plaintext: bytes) -> bytes:
    import ctypes

    crypt32, kernel32, data_blob = _load_crypt32()
    src = (ctypes.c_ubyte * len(plaintext)).from_buffer_copy(plaintext)
    entropy = (ctypes.c_ubyte * len(_ENTROPY)).from_buffer_copy(_ENTROPY)
    in_blob = data_blob(len(plaintext), ctypes.cast(src, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_blob = data_blob(
        len(_ENTROPY), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte))
    )
    out_blob = data_blob()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, ctypes.byref(entropy_blob), None, None,
        _UI_FORBIDDEN, ctypes.byref(out_blob),
    ):
        raise KeyStoreError("DPAPI 加密失败")
    try:
        return ctypes.string_at(out_blob.pbData, int(out_blob.cbData))
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect_bytes(ciphertext: bytes) -> bytes:
    import ctypes

    crypt32, kernel32, data_blob = _load_crypt32()
    src = (ctypes.c_ubyte * len(ciphertext)).from_buffer_copy(ciphertext)
    entropy = (ctypes.c_ubyte * len(_ENTROPY)).from_buffer_copy(_ENTROPY)
    in_blob = data_blob(len(ciphertext), ctypes.cast(src, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_blob = data_blob(
        len(_ENTROPY), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte))
    )
    out_blob = data_blob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, ctypes.byref(entropy_blob), None, None,
        _UI_FORBIDDEN, ctypes.byref(out_blob),
    ):
        raise KeyStoreDecryptError("密钥密文无法解密")
    try:
        return ctypes.string_at(out_blob.pbData, int(out_blob.cbData))
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _restrict_key_file(path: str) -> None:
    """只给当前用户保留密钥文件读写权限。"""
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
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID,
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
            process_token, token_user_class, None, 0, ctypes.byref(required)
        )
        token_buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            process_token, token_user_class, token_buffer, required, ctypes.byref(required)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.cast(token_buffer, ctypes.POINTER(TokenUser)).contents
        if not advapi32.ConvertSidToStringSidW(user.user.sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = f"D:P(A;;FA;;;{sid_text.value})"
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(security_descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi32.SetFileSecurityW(
            path,
            dacl_security_information | protected_dacl_security_information,
            security_descriptor,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError as error:
        raise KeyStoreError("无法设置密钥文件权限") from error
    finally:
        if security_descriptor:
            kernel32.LocalFree(security_descriptor)
        if sid_text:
            kernel32.LocalFree(sid_text)
        if process_token:
            kernel32.CloseHandle(process_token)


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """先限制空文件权限，再写入并原子替换密钥文件。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(
        directory, f".{os.path.basename(path)}.{secrets.token_hex(8)}.tmp"
    )
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        _restrict_key_file(tmp_path)
        with open(tmp_path, "wb") as key_file:
            key_file.write(data)
            key_file.flush()
            os.fsync(key_file.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


@contextmanager
def _secret_service_collection():
    try:
        import secretstorage

        connection = secretstorage.dbus_init()
    except Exception as error:
        raise KeyStoreUnsupportedError("Secret Service 不可用") from error

    try:
        collection = secretstorage.get_default_collection(connection)
        # secretstorage 解锁成功返回 False，取消返回 True
        if collection.is_locked() and collection.unlock():
            raise KeyStoreUnsupportedError("Secret Service 集合已锁定")
        yield collection
    except KeyStoreError:
        raise
    except Exception as error:
        raise KeyStoreUnsupportedError("Secret Service 不可用") from error
    finally:
        connection.close()


def _find_secret_service_item(collection):
    if collection is None:
        raise KeyStoreUnsupportedError("Secret Service 不可用")
    try:
        for item in collection.search_items(_SECRET_SERVICE_ATTRIBUTES):
            return item
    except Exception as error:
        raise KeyStoreError("读取 Secret Service 密钥失败") from error
    return None


def _save_secret_service_key(key: str) -> None:
    with _secret_service_collection() as collection:
        try:
            collection.create_item(
                _SECRET_SERVICE_LABEL,
                _SECRET_SERVICE_ATTRIBUTES,
                key.encode("utf-8"),
                replace=True,
            )
        except Exception as error:
            raise KeyStoreError("写入 Secret Service 密钥失败") from error


def _load_secret_service_key() -> str | None:
    try:
        with _secret_service_collection() as collection:
            item = _find_secret_service_item(collection)
            if item is None:
                return None
            try:
                if item.is_locked() and item.unlock():
                    raise KeyStoreError("Secret Service 密钥已锁定")
                plaintext = item.get_secret()
                return plaintext.decode("utf-8")
            except KeyStoreError:
                raise
            except UnicodeDecodeError as error:
                raise KeyStoreDecryptError("密钥密文无法解密") from error
            except Exception as error:
                raise KeyStoreError("读取 Secret Service 密钥失败") from error
    except KeyStoreUnsupportedError:
        return None


def _delete_secret_service_key() -> None:
    try:
        with _secret_service_collection() as collection:
            item = _find_secret_service_item(collection)
            if item is None:
                return
            try:
                item.delete()
            except Exception as error:
                raise KeyStoreError("删除 Secret Service 密钥失败") from error
    except KeyStoreUnsupportedError:
        return


def _secret_service_status() -> KeyStoreStatus:
    try:
        with _secret_service_collection() as collection:
            item = _find_secret_service_item(collection)
            if item is None:
                return KeyStoreStatus.ABSENT
            if item.is_locked():
                return KeyStoreStatus.STORED
            try:
                item.get_secret().decode("utf-8")
            except UnicodeDecodeError:
                return KeyStoreStatus.CORRUPT
            except Exception:
                return KeyStoreStatus.CORRUPT
            return KeyStoreStatus.STORED
    except KeyStoreUnsupportedError:
        return KeyStoreStatus.UNSUPPORTED


def _validate_key(key: str) -> str:
    normalized_key = key.strip()
    if not normalized_key:
        raise KeyStoreInvalidKeyError("API key 不能为空")
    if len(normalized_key) > _MAX_KEY_LENGTH:
        raise KeyStoreInvalidKeyError(f"API key 超过 {_MAX_KEY_LENGTH} 字符上限")
    return normalized_key


class AIKeyStore:
    def __init__(self, key_file: str | os.PathLike[str]) -> None:
        self._key_file = Path(key_file)

    def supports_persistence(self) -> bool:
        return _supports_persistence()

    def save_api_key(self, key: str) -> None:
        normalized_key = _validate_key(key)
        if _is_windows():
            _atomic_write_bytes(
                str(self._key_file),
                _protect_bytes(normalized_key.encode("utf-8")),
            )
            return
        _save_secret_service_key(normalized_key)

    def load_api_key(self) -> str | None:
        if not _is_windows():
            return _load_secret_service_key()
        try:
            ciphertext = self._key_file.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise KeyStoreError("读取密钥文件失败") from error
        plaintext = _unprotect_bytes(ciphertext)
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as error:
            raise KeyStoreDecryptError("密钥密文无法解密") from error

    def delete_api_key(self) -> None:
        if not _is_windows():
            _delete_secret_service_key()
            return
        try:
            self._key_file.unlink()
        except FileNotFoundError:
            pass

    def api_key_status(self) -> KeyStoreStatus:
        if not _is_windows():
            return _secret_service_status()
        try:
            ciphertext = self._key_file.read_bytes()
        except FileNotFoundError:
            return KeyStoreStatus.ABSENT
        except OSError:
            return KeyStoreStatus.CORRUPT
        try:
            _unprotect_bytes(ciphertext)
        except KeyStoreDecryptError:
            return KeyStoreStatus.CORRUPT
        return KeyStoreStatus.STORED
