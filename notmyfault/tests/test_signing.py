"""插件签名：密钥加载、文件清单与签名往返"""

import hashlib

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from notmyfault.security import signing


def make_key():
    return ed25519.Ed25519PrivateKey.generate()


class TestPluginFiles:
    def test_empty_dir(self, tmp_path):
        assert signing.plugin_files(tmp_path) == []

    def test_only_sig_file(self, tmp_path):
        (tmp_path / "signature.sig").write_bytes(b"sig")
        assert signing.plugin_files(tmp_path) == []

    def test_returns_py_and_json_sorted(self, tmp_path):
        (tmp_path / "b.py").write_text("x = 1")
        (tmp_path / "a.json").write_text("{}")
        (tmp_path / "c.txt").write_text("ignored")
        (tmp_path / "signature.sig").write_bytes(b"sig")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "d.json").write_text("{}")
        files = signing.plugin_files(tmp_path)
        rel = [f.relative_to(tmp_path).as_posix() for f in files]
        assert rel == ["a.json", "b.py", "sub/d.json"]


class TestLoadPrivateKey:
    def test_load_unencrypted_pem(self, tmp_path):
        key = make_key()
        pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        path = tmp_path / "key.pem"
        path.write_bytes(pem)
        loaded = signing.load_private_key(path)
        message = b"pem-key-check"
        key.public_key().verify(loaded.sign(message), message)

    def test_load_encrypted_pem(self, tmp_path):
        key = make_key()
        pem = key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, BestAvailableEncryption(b"secret-pw")
        )
        path = tmp_path / "key.pem"
        path.write_bytes(pem)
        loaded = signing.load_private_key(path, password="secret-pw")
        message = b"roundtrip"
        sig = loaded.sign(message)
        key.public_key().verify(sig, message)

    def test_load_raw_format(self, tmp_path):
        key = make_key()
        raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        path = tmp_path / "key.raw"
        path.write_bytes(raw)
        loaded = signing.load_private_key(path)
        message = b"raw-key-check"
        key.public_key().verify(loaded.sign(message), message)


class TestSignPlugin:
    def test_signs_and_writes_sig(self, tmp_path):
        (tmp_path / "action.json").write_text('{"id": "demo"}', encoding="utf-8")
        (tmp_path / "action.py").write_text("def run(params):\n    return True\n")
        assert signing.sign_plugin(tmp_path, "action.json", private_key=make_key())
        sig_file = tmp_path / "signature.sig"
        assert sig_file.exists()
        assert len(sig_file.read_bytes()) == 64

    def test_verify_roundtrip(self, tmp_path):
        (tmp_path / "action.json").write_text('{"id": "demo"}', encoding="utf-8")
        (tmp_path / "action.py").write_text("VALUE = 1\n")
        key = make_key()
        signing.sign_plugin(tmp_path, "action.json", private_key=key)

        payload = b"".join(f.read_bytes() for f in signing.plugin_files(tmp_path))
        digest = hashlib.sha256(payload).digest()
        sig = (tmp_path / "signature.sig").read_bytes()
        key.public_key().verify(sig, digest)

        # 篡改任意文件后原签名必须失效
        (tmp_path / "action.py").write_text("VALUE = 2\n")
        payload = b"".join(f.read_bytes() for f in signing.plugin_files(tmp_path))
        with pytest.raises(InvalidSignature):
            key.public_key().verify(sig, hashlib.sha256(payload).digest())


class TestKeyStatus:
    def test_no_key_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(signing, "PRIVATE_KEY_FILE", tmp_path / "missing.pem")
        assert signing.key_status() == {"exists": False, "encrypted": False}

    def test_plain_key(self, tmp_path, monkeypatch):
        path = tmp_path / "key.pem"
        path.write_bytes(b"-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYD")
        monkeypatch.setattr(signing, "PRIVATE_KEY_FILE", path)
        assert signing.key_status() == {"exists": True, "encrypted": False}

    def test_encrypted_key(self, tmp_path, monkeypatch):
        path = tmp_path / "key.pem"
        path.write_bytes(b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nMC4CAQAw")
        monkeypatch.setattr(signing, "PRIVATE_KEY_FILE", path)
        assert signing.key_status() == {"exists": True, "encrypted": True}

    def test_raw_key(self, tmp_path, monkeypatch):
        key = make_key()
        path = tmp_path / "key.raw"
        path.write_bytes(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
        monkeypatch.setattr(signing, "PRIVATE_KEY_FILE", path)
        assert signing.key_status() == {"exists": True, "encrypted": False}
