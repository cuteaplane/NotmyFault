import hashlib
from pathlib import Path
from typing import Optional

_PRIVATE_DIR = Path(__file__).resolve().parents[2] / ".private"
PRIVATE_KEY_FILE = _PRIVATE_DIR / "signing_private_key.pem"


def _get_crypto():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption,
        BestAvailableEncryption, load_pem_private_key,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return ed25519, Encoding, PrivateFormat, PublicFormat, NoEncryption, BestAvailableEncryption, load_pem_private_key, Ed25519PrivateKey


def plugin_files(plugin_dir) -> list[Path]:
    """返回按相对路径排序的 .py 和 .json 文件并排除签名文件，sign_plugin 与 verify_plugin_sig 必须使用同一清单。"""
    files = []
    for f in sorted(
        Path(plugin_dir).rglob("*"),
        key=lambda path: path.relative_to(plugin_dir).as_posix(),
    ):
        if f.is_file() and f.suffix in (".py", ".json") and f.name != "signature.sig":
            files.append(f)
    return files


def _plugin_files(plugin_dir: Path, json_name: str) -> list[Path]:
    # 保留旧的 json_name 参数，实际清单统一由 plugin_files 生成。
    return plugin_files(plugin_dir)


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
    files = plugin_files(plugin_dir)
    payload = b"".join(f.read_bytes() for f in files)
    sig = private_key.sign(hashlib.sha256(payload).digest())
    (plugin_dir / "signature.sig").write_bytes(sig)
    return True


def sign_file(path, private_key) -> None:
    """对文件的 SHA-256 摘要做 Ed25519 签名，并写入同名的 .sig 文件。"""
    data = Path(path).read_bytes()
    sig = private_key.sign(hashlib.sha256(data).digest())
    Path(str(path) + ".sig").write_bytes(sig)


def verify_file(path) -> bool:
    """校验文件旁的 .sig 文件，缺少签名或校验失败都返回 False。"""
    sig_path = Path(str(path) + ".sig")
    if not sig_path.exists():
        return False
    try:
        from notmyfault.security.signing_keys import get_public_keys
        pub_keys = get_public_keys()
        if not pub_keys:
            return False
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pubs = [Ed25519PublicKey.from_public_bytes(k) for k in pub_keys]
    except ImportError:
        return False
    try:
        data = Path(path).read_bytes()
        sig = sig_path.read_bytes()
    except OSError:
        return False
    digest = hashlib.sha256(data).digest()
    for pub in pubs:
        try:
            pub.verify(sig, digest)
            return True
        except Exception:
            continue
    return False


def key_status() -> dict:
    if not PRIVATE_KEY_FILE.exists():
        return {"exists": False, "encrypted": False}
    header = PRIVATE_KEY_FILE.read_bytes()[:20]
    encrypted = header.startswith(b"-----BEGIN ENCRYPTED")
    return {"exists": True, "encrypted": encrypted}
