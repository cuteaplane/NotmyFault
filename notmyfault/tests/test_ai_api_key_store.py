"""DPAPI AI API 密钥存储的往返、加密、异常与平台行为。"""

import base64
import binascii
from contextlib import contextmanager
import os

import pytest

from notmyfault.security import api_key_store as store


@pytest.fixture
def fake_dpapi(monkeypatch):
    """把 DPAPI 替换成 base64，密文不含明文，测试不依赖真实 Windows DPAPI。"""

    def protect(plaintext: bytes) -> bytes:
        return base64.b64encode(plaintext)

    def unprotect(ciphertext: bytes) -> bytes:
        try:
            return base64.b64decode(ciphertext, validate=True)
        except (binascii.Error, ValueError) as error:
            raise store.KeyStoreDecryptError("密文损坏") from error

    monkeypatch.setattr(store, "_protect_bytes", protect)
    monkeypatch.setattr(store, "_unprotect_bytes", unprotect)


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """把密钥文件指到临时目录，并把 ACL 换成空操作。"""
    path = tmp_path / ".ai_api_key"
    monkeypatch.setattr(store, "_key_file_path", lambda: str(path))
    monkeypatch.setattr(store, "_restrict_key_file", lambda candidate: None)
    return path


@pytest.fixture(autouse=True)
def dpapi_backend(monkeypatch):
    monkeypatch.setattr(store, "_is_windows", lambda: True)


class TestSupportsPersistence:
    def test_windows_is_supported(self, monkeypatch):
        monkeypatch.setattr(store, "_is_windows", lambda: True)
        assert store.supports_persistence() is True

    def test_non_windows_without_secret_service_is_unsupported(self, monkeypatch):
        monkeypatch.setattr(store, "_is_windows", lambda: False)

        @contextmanager
        def unavailable():
            raise store.KeyStoreUnsupportedError("Secret Service 不可用")
            yield

        monkeypatch.setattr(store, "_secret_service_collection", unavailable)
        assert store.supports_persistence() is False


class TestSaveAndLoad:
    def test_round_trip(self, isolated_store, fake_dpapi):
        key = "sk-test-secret-12345"
        store.save_api_key(key)
        assert store.load_api_key() == key

    def test_encrypted_at_rest_no_plaintext(self, isolated_store, fake_dpapi):
        key = "sk-plaintext-must-not-leak"
        store.save_api_key(key)
        assert key.encode("utf-8") not in isolated_store.read_bytes()

    def test_overwrite_replaces_previous(self, isolated_store, fake_dpapi):
        store.save_api_key("first-key")
        store.save_api_key("second-key")
        assert store.load_api_key() == "second-key"

    def test_leading_and_trailing_whitespace_is_removed(
        self, isolated_store, fake_dpapi
    ):
        store.save_api_key("  sk-test-secret  \r\n")
        assert store.load_api_key() == "sk-test-secret"

    def test_no_temp_file_residue(self, isolated_store, fake_dpapi, tmp_path):
        store.save_api_key("secret")
        assert not list(tmp_path.glob("*.tmp"))

    def test_restrict_runs_on_empty_tmp_before_write(
        self, monkeypatch, tmp_path, fake_dpapi
    ):
        monkeypatch.setattr(
            store, "_key_file_path", lambda: str(tmp_path / ".ai_api_key")
        )
        seen = []

        def restrict(candidate):
            with open(candidate, "rb") as candidate_file:
                assert candidate_file.read() == b""
            seen.append(candidate)

        monkeypatch.setattr(store, "_restrict_key_file", restrict)
        store.save_api_key("secret")
        assert len(seen) == 1


class TestAbsentAndCorrupt:
    def test_load_absent_returns_none(self, isolated_store, fake_dpapi):
        assert store.load_api_key() is None

    def test_status_absent(self, isolated_store, fake_dpapi):
        assert store.api_key_status() is store.KeyStoreStatus.ABSENT

    def test_load_corrupt_raises(self, isolated_store, fake_dpapi):
        isolated_store.write_bytes(b"not-a-valid-encrypted-blob")
        with pytest.raises(store.KeyStoreDecryptError):
            store.load_api_key()

    def test_status_corrupt(self, isolated_store, fake_dpapi):
        isolated_store.write_bytes(b"garbage")
        assert store.api_key_status() is store.KeyStoreStatus.CORRUPT

    def test_status_stored(self, isolated_store, fake_dpapi):
        store.save_api_key("secret")
        assert store.api_key_status() is store.KeyStoreStatus.STORED


class TestDelete:
    def test_delete_is_idempotent_when_absent(self, isolated_store, fake_dpapi):
        store.delete_api_key()
        store.delete_api_key()
        assert not isolated_store.exists()

    def test_delete_removes_then_again(self, isolated_store, fake_dpapi):
        store.save_api_key("secret")
        assert isolated_store.exists()
        store.delete_api_key()
        assert not isolated_store.exists()
        store.delete_api_key()
        assert not isolated_store.exists()
        assert store.load_api_key() is None


class TestInvalidKey:
    def test_empty_key_rejected(self, isolated_store, fake_dpapi):
        with pytest.raises(store.KeyStoreInvalidKeyError):
            store.save_api_key("")

    def test_whitespace_only_key_rejected(self, isolated_store, fake_dpapi):
        with pytest.raises(store.KeyStoreInvalidKeyError):
            store.save_api_key("   ")

    def test_oversized_key_rejected(self, isolated_store, fake_dpapi):
        with pytest.raises(store.KeyStoreInvalidKeyError):
            store.save_api_key("x" * (store._MAX_KEY_LENGTH + 1))

    def test_max_length_key_accepted(self, isolated_store, fake_dpapi):
        key = "x" * store._MAX_KEY_LENGTH
        store.save_api_key(key)
        assert store.load_api_key() == key


class TestUnsupportedPlatform:
    def test_save_raises_unsupported(self, monkeypatch):
        monkeypatch.setattr(store, "_is_windows", lambda: False)
        self._disable_secret_service(monkeypatch)
        with pytest.raises(store.KeyStoreUnsupportedError):
            store.save_api_key("secret")

    def test_load_returns_none_on_unsupported(self, monkeypatch):
        monkeypatch.setattr(store, "_is_windows", lambda: False)
        self._disable_secret_service(monkeypatch)
        assert store.load_api_key() is None

    def test_status_unsupported(self, monkeypatch):
        monkeypatch.setattr(store, "_is_windows", lambda: False)
        self._disable_secret_service(monkeypatch)
        assert store.api_key_status() is store.KeyStoreStatus.UNSUPPORTED

    def test_delete_noop_on_unsupported(self, monkeypatch):
        monkeypatch.setattr(store, "_is_windows", lambda: False)
        self._disable_secret_service(monkeypatch)
        store.delete_api_key()

    @staticmethod
    def _disable_secret_service(monkeypatch):
        @contextmanager
        def unavailable():
            raise store.KeyStoreUnsupportedError("Secret Service 不可用")
            yield

        monkeypatch.setattr(store, "_secret_service_collection", unavailable)


class TestRealDpapi:
    @pytest.mark.skipif(os.name != "nt", reason="需要 Windows")
    def test_real_dpapi_round_trip(self, tmp_path, monkeypatch):
        path = tmp_path / ".ai_api_key"
        monkeypatch.setattr(store, "_key_file_path", lambda: str(path))
        key = "sk-real-dpapi-round-trip"
        store.save_api_key(key)
        assert store.load_api_key() == key
        assert key.encode("utf-8") not in path.read_bytes()
        store.delete_api_key()
        assert not path.exists()


class TestSecretService:
    def test_round_trip_replaces_item_and_keeps_attributes(self, monkeypatch):
        collection = FakeSecretCollection()
        self._use_collection(monkeypatch, collection)

        store.save_api_key("  sk-linux-secret  ")
        store.save_api_key("sk-linux-secret-2")

        assert store.load_api_key() == "sk-linux-secret-2"
        assert collection.labels == [
            "NotmyFault AI API Key",
            "NotmyFault AI API Key",
        ]
        assert collection.item.attributes == {
            "application": "NotmyFault",
            "purpose": "ai_api_key",
        }
        assert collection.item.secrets == [
            b"sk-linux-secret",
            b"sk-linux-secret-2",
        ]

    def test_absent_and_delete_are_idempotent(self, monkeypatch):
        collection = FakeSecretCollection()
        self._use_collection(monkeypatch, collection)

        assert store.api_key_status() is store.KeyStoreStatus.ABSENT
        assert store.load_api_key() is None
        store.delete_api_key()

        store.save_api_key("sk-to-delete")
        item = collection.item
        store.delete_api_key()
        assert item.deleted is True
        assert store.api_key_status() is store.KeyStoreStatus.ABSENT

    def test_locked_item_reports_stored_and_load_unlocks(self, monkeypatch):
        collection = FakeSecretCollection()
        collection.item = FakeSecretItem(b"sk-locked", locked=True)
        self._use_collection(monkeypatch, collection)

        assert store.api_key_status() is store.KeyStoreStatus.STORED
        assert store.load_api_key() == "sk-locked"
        assert collection.item.unlock_calls == 1

    def test_invalid_key_is_rejected_before_keyring_write(self, monkeypatch):
        collection = FakeSecretCollection()
        self._use_collection(monkeypatch, collection)

        with pytest.raises(store.KeyStoreInvalidKeyError):
            store.save_api_key("   ")
        assert collection.item is None

    @staticmethod
    def _use_collection(monkeypatch, collection):
        monkeypatch.setattr(store, "_is_windows", lambda: False)

        @contextmanager
        def fake_collection():
            yield collection

        monkeypatch.setattr(store, "_secret_service_collection", fake_collection)


class FakeSecretItem:
    def __init__(self, secret, locked=False):
        self.label = "NotmyFault AI API Key"
        self.attributes = {
            "application": "NotmyFault",
            "purpose": "ai_api_key",
        }
        self.secrets = [secret]
        self.locked = locked
        self.unlock_calls = 0
        self.deleted = False

    def is_locked(self):
        return self.locked

    def unlock(self):
        self.unlock_calls += 1
        self.locked = False
        return False

    def get_secret(self):
        return self.secrets[-1]

    def delete(self):
        self.deleted = True


class FakeSecretCollection:
    def __init__(self):
        self.item = None
        self.labels = []

    def create_item(self, label, attributes, secret, replace=False, content_type="text/plain"):
        assert replace is True
        assert content_type == "text/plain"
        self.labels.append(label)
        if self.item is None:
            self.item = FakeSecretItem(secret)
        else:
            self.item.attributes = attributes
            self.item.secrets.append(secret)
        return self.item

    def search_items(self, attributes):
        if self.item is None or self.item.deleted:
            return iter(())
        assert self.item.attributes == attributes
        return iter((self.item,))
