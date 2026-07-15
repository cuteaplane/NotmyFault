import hashlib
import os
import sys
from pathlib import Path
from typing import Optional

_PRIVATE_DIR = Path(__file__).resolve().parent.parent / ".private"
PRIVATE_KEY_FILE = _PRIVATE_DIR / "signing_private_key.pem"


def _get_crypto():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption,
        BestAvailableEncryption, load_pem_private_key,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return ed25519, Encoding, PrivateFormat, PublicFormat, NoEncryption, BestAvailableEncryption, load_pem_private_key, Ed25519PrivateKey


def _plugin_files(plugin_dir: Path, json_name: str) -> list[Path]:
    files = []
    for f in sorted(plugin_dir.iterdir()):
        if f.is_file() and f.suffix in (".py", ".json") and f.name != "signature.sig":
            files.append(f)
    return files


def load_private_key(path: Path, password: Optional[str] = None):
    _, _, _, _, _, _, load_pem_private_key, Ed25519PrivateKey = _get_crypto()
    data = path.read_bytes()

    if data.startswith(b"-----BEGIN "):
        try:
            return load_pem_private_key(data, password=None)
        except Exception:
            if password is not None:
                return load_pem_private_key(data, password=password.encode())
            import getpass
            pw = getpass.getpass("请输入签名密码: ")
            return load_pem_private_key(data, password=pw.encode())

    return Ed25519PrivateKey.from_private_bytes(data)


def sign_plugin(plugin_dir: Path, json_name: str, private_key=None) -> bool:
    if private_key is None:
        private_key = load_private_key(PRIVATE_KEY_FILE)
    files = _plugin_files(plugin_dir, json_name)
    payload = b"".join(f.read_bytes() for f in files)
    sig = private_key.sign(hashlib.sha256(payload).digest())
    (plugin_dir / "signature.sig").write_bytes(sig)
    return True


def key_status() -> dict:
    if not PRIVATE_KEY_FILE.exists():
        return {"exists": False, "encrypted": False}
    header = PRIVATE_KEY_FILE.read_bytes()[:20]
    encrypted = header.startswith(b"-----BEGIN ENCRYPTED")
    return {"exists": True, "encrypted": encrypted}
