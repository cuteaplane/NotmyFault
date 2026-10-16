"""插件签名：密钥加载、文件清单与签名往返"""

import hashlib
import os

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from notmyfault.security import signing


def make_key():
    return ed25519.Ed25519PrivateKey.generate()


def test_private_key_replacement_preserves_previous_file_on_failure(tmp_path, monkeypatch):
    import build

    path = tmp_path / "key.pem"
    old_key = make_key()
    build._save_private_key(old_key, path)
    previous = path.read_bytes()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(build.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        build._save_private_key(make_key(), path)
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def test_file_signature_uses_valid_key_when_another_key_is_malformed(tmp_path, monkeypatch):
    from notmyfault.security import signing_keys

    key = make_key()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    assert signing.load_public_key_bytes(pem) == public
    assert signing.load_public_key_bytes(public) == public
    path = tmp_path / "data"
    path.write_bytes(b"signed content")
    signing.sign_file(path, key)
    monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [b"invalid", public])
    assert signing.verify_file(path)
    path.write_bytes(b"changed")
    assert not signing.verify_file(path)


class TestPluginFiles:
    def test_empty_dir(self, tmp_path):
        assert signing.plugin_files(tmp_path) == []

    def test_only_sig_file(self, tmp_path):
        (tmp_path / "signature.sig").write_bytes(b"sig")
        assert signing.plugin_files(tmp_path) == []

    def test_returns_all_regular_files_sorted(self, tmp_path):
        (tmp_path / "b.py").write_text("x = 1")
        (tmp_path / "a.json").write_text("{}")
        (tmp_path / "c.txt").write_text("included")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "tool.exe").write_bytes(b"\x00")
        (tmp_path / "signature.sig").write_bytes(b"sig")
        (tmp_path / "public_key.pem").write_text("key")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "d.json").write_text("{}")
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "e.py").write_text("VALUE = 1")
        for directory in ("node_modules", "__pypackages__", "__pycache__"):
            dependency = tmp_path / directory
            dependency.mkdir()
            (dependency / "helper.py").write_text("VALUE = 2")
        files = signing.plugin_files(tmp_path)
        rel = [f.relative_to(tmp_path).as_posix() for f in files]
        assert rel == [
            ".hidden/e.py", "__pypackages__/helper.py", "a.json", "b.py", "bin/tool.exe",
            "c.txt", "node_modules/helper.py", "sub/d.json"
        ]


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
        assert signing.sign_plugin(tmp_path, private_key=make_key())
        sig_file = tmp_path / "signature.sig"
        assert sig_file.exists()
        assert len(sig_file.read_bytes()) == 64

    def test_verify_roundtrip(self, tmp_path):
        (tmp_path / "action.json").write_text('{"id": "demo"}', encoding="utf-8")
        (tmp_path / "action.py").write_text("VALUE = 1\n")
        key = make_key()
        signing.sign_plugin(tmp_path, private_key=key)

        payload = signing.plugin_payload(tmp_path)
        digest = hashlib.sha256(payload).digest()
        sig = (tmp_path / "signature.sig").read_bytes()
        key.public_key().verify(sig, digest)

        # 篡改任意文件后原签名必须失效
        (tmp_path / "action.py").write_text("VALUE = 2\n")
        payload = signing.plugin_payload(tmp_path)
        with pytest.raises(InvalidSignature):
            key.public_key().verify(sig, hashlib.sha256(payload).digest())

    def test_payload_binds_file_names_and_boundaries(self, tmp_path):
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_bytes(b"ab")
        second.write_bytes(b"c")
        original = signing.plugin_payload(tmp_path)

        first.write_bytes(b"a")
        second.write_bytes(b"bc")
        moved_boundary = signing.plugin_payload(tmp_path)
        assert moved_boundary != original

        second.rename(tmp_path / "renamed.txt")
        renamed = signing.plugin_payload(tmp_path)
        assert renamed != moved_boundary


class TestCounterSignAuthorKey:
    def test_counter_sign_roundtrip(self, tmp_path):
        author_key = make_key()
        user_key = make_key()
        user_pub = user_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        signing.export_public_key(author_key, tmp_path / "public_key.pem")
        signing.counter_sign_author_key(tmp_path, private_key=user_key)
        assert (tmp_path / "public_key.sig").exists()
        assert signing.verify_author_key_counter_signature(tmp_path, [user_pub])
        assert not signing.verify_author_key_counter_signature(
            tmp_path, [author_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)]
        )

    def test_missing_counter_signature_fails(self, tmp_path):
        author_key = make_key()
        signing.export_public_key(author_key, tmp_path / "public_key.pem")
        assert not signing.verify_author_key_counter_signature(tmp_path, [b"\x00" * 32])


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


@pytest.mark.parametrize("mode,encrypted,allowed", [
    ("strict", True, True), ("strict", False, False),
    ("permissive", True, True), ("permissive", False, True),
])
def test_build_checks_existing_key_without_replacing_it(tmp_path, monkeypatch, mode, encrypted, allowed):
    import build

    key_path = tmp_path / "key.pem"
    encryption = BestAvailableEncryption(b"test-password") if encrypted else NoEncryption()
    original = make_key().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, encryption)
    key_path.write_bytes(original)
    monkeypatch.setattr(build, "PRIVATE_KEY_FILE", key_path)
    if allowed:
        build._ensure_key_for_build(mode)
    else:
        with pytest.raises(SystemExit) as failure:
            build._ensure_key_for_build(mode)
        assert failure.value.code == 1
    assert key_path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [key_path]


@pytest.mark.parametrize("mode", ["strict", "permissive"])
def test_build_missing_key_does_not_generate_one(tmp_path, monkeypatch, mode):
    import build

    key_path = tmp_path / "key.pem"
    monkeypatch.setattr(build, "PRIVATE_KEY_FILE", key_path)
    with pytest.raises(SystemExit) as failure:
        build._ensure_key_for_build(mode)
    assert failure.value.code == 1
    assert not key_path.exists()


def test_build_rejects_normal_before_signing(monkeypatch):
    import build

    monkeypatch.setattr(build, "cmd_sign", lambda args: pytest.fail("unexpected signing"))
    with pytest.raises(SystemExit) as failure:
        build.cmd_build({"security_mode": "normal"})
    assert failure.value.code == 1


def test_plugin_signature_rejects_removed_local_signature_fallback(tmp_path, monkeypatch):
    from notmyfault.security import plugins, signing_keys

    (tmp_path / "action.json").write_text('{"id":"demo"}', encoding="utf-8")
    (tmp_path / "action.py").write_text('VALUE = 1\n', encoding="utf-8")
    key = make_key()
    monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ])
    signing.sign_plugin(tmp_path, private_key=key)
    assert plugins.plugin_signature_kind(str(tmp_path), "builtin") == "official"
    assert plugins.plugin_signature_kind(str(tmp_path), "user") == "none"
    signing.self_sign_plugin(tmp_path, key)
    assert plugins.plugin_signature_kind(str(tmp_path), "user") == "author"
    old_payload = b"".join(path.read_bytes() for path in signing.plugin_files(tmp_path))
    (tmp_path / "signature.sig").write_bytes(key.sign(hashlib.sha256(old_payload).digest()))
    assert plugins.plugin_signature_kind(str(tmp_path), "user") == "none"


def test_build_force_does_not_replace_existing_key(tmp_path, monkeypatch):
    import build

    key_path = tmp_path / "key.pem"
    original = make_key().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    key_path.write_bytes(original)
    monkeypatch.setattr(build, "PRIVATE_KEY_FILE", key_path)
    with pytest.raises(SystemExit) as failure:
        build._ensure_key_for_build("permissive", force=True)
    assert failure.value.code == 1
    assert key_path.read_bytes() == original
