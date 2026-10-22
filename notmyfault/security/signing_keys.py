"""保存发行公钥和初始化时固定的本地签名公钥。"""

from typing import Optional

BUILTIN_PUBLIC_KEY: bytes = b'\xc1\xad\xe1\xdds\xf1\xe8\xb2\x7fV\xb0\xda\xfe_\xb2\x12\x86\xb0Bg.=m\xdc\xa1p\\\xca\xda\xe9+\x87'

USER_PUBLIC_KEY: Optional[bytes] = None


def get_public_keys() -> list[bytes]:
    return [key for key in (BUILTIN_PUBLIC_KEY, USER_PUBLIC_KEY) if key]
