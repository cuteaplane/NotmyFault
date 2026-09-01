"""保存插件签名公钥，内置公钥写在源码中，用户公钥从 .private/signing_public.pem 读取。"""

import os
from typing import Optional

CURDIR = os.path.dirname(os.path.abspath(__file__))
_PRIVATE_DIR = os.path.normpath(os.path.join(CURDIR, "..", "..", ".private"))

BUILTIN_PUBLIC_KEY: bytes = b'\xc1\xad\xe1\xdds\xf1\xe8\xb2\x7fV\xb0\xda\xfe_\xb2\x12\x86\xb0Bg.=m\xdc\xa1p\\\xca\xda\xe9+\x87'

_USER_PUBLIC_KEY_PATH = os.path.join(_PRIVATE_DIR, "signing_public.pem")


def _load_user_public_key() -> Optional[bytes]:
    try:
        with open(_USER_PUBLIC_KEY_PATH, "rb") as f:
            return f.read()
    except (OSError, FileNotFoundError):
        return None


USER_PUBLIC_KEY: Optional[bytes] = _load_user_public_key()


def get_public_keys() -> list[bytes]:
    keys: list[bytes] = []
    if BUILTIN_PUBLIC_KEY:
        keys.append(BUILTIN_PUBLIC_KEY)
    if USER_PUBLIC_KEY:
        keys.append(USER_PUBLIC_KEY)
    return keys
