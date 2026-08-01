import hashlib
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


def plugin_files(plugin_dir) -> list[Path]:
    """参与签名/校验的插件文件：目录下排序后的 .py/.json 文件（排除 signature.sig）。

    签名（sign_plugin）与校验（plugins.verify_plugin_sig）必须用同一份文件清单，
    否则两边算出的 payload 不一致，会让合法签名被当成篡改——这正是之前的 bug：
    签名只哈希 .py/.json，校验却哈希了目录下所有文件，多放一个 README/icon 就校验失败。
    """
    files = []
    for f in sorted(
        Path(plugin_dir).rglob("*"),
        key=lambda path: path.relative_to(plugin_dir).as_posix(),
    ):
        if f.is_file() and f.suffix in (".py", ".json") and f.name != "signature.sig":
            files.append(f)
    return files


def _plugin_files(plugin_dir: Path, json_name: str) -> list[Path]:
    # 兼容旧调用（json_name 历史上未使用），统一走 plugin_files。
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
    """对任意文件签名（Ed25519 over sha256(bytes)），写入 <path>.sig。

    用于 build.json（防止有人改 security_mode 降级安全模式）和 integrity.json
    （核心源码哈希清单，防止篡改 engine.py/config.py 等）。
    """
    data = Path(path).read_bytes()
    sig = private_key.sign(hashlib.sha256(data).digest())
    Path(str(path) + ".sig").write_bytes(sig)


def verify_file(path) -> bool:
    """校验 <path>.sig。无签名文件或校验失败都返回 False。

    调用方据此把该文件当作不可信（跳过/回退兜底），这样既挡得住篡改（签名无效），
    也要求 fresh clone 必须先跑 build.py 生成签名（签名缺失）。
    """
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
