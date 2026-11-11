from decimal import Decimal

import pytest

from notmyfault.core.state_store import StateStore


def test_state_is_copied_and_survives_reopen(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    store = StateStore(path)
    value = {"history": [Decimal("0.125")]}
    store.set("demo", "baseline", value, durable=True)
    value["history"].append("外部修改")
    saved = store.get("demo", "baseline")
    assert saved == {"history": [Decimal("0.125")]}
    saved["history"].clear()
    assert store.get("demo", "baseline") == {"history": [Decimal("0.125")]}
    assert store.get("other", "baseline") is None
    assert store.close()
    reopened = StateStore(path)
    try:
        assert reopened.get("demo", "baseline") == {"history": [Decimal("0.125")]}
    finally:
        assert reopened.close()


def test_writer_failure_reaches_caller_and_store_still_reads(tmp_path):
    import sqlite3
    from contextlib import closing

    path = tmp_path / "runtime.sqlite3"
    store = StateStore(path)
    store.set("demo", "baseline", 1, durable=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE trigger_state")
        connection.commit()
    try:
        with pytest.raises(RuntimeError, match="状态写入失败"):
            store.set("demo", "baseline", 2, durable=True)
        assert store.get("demo", "baseline") == 2
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "CREATE TABLE trigger_state (scope TEXT, key TEXT, value TEXT, PRIMARY KEY(scope,key))"
            )
            connection.commit()
        store.set("demo", "baseline", 3, durable=True)
        restored = StateStore(path)
        try:
            assert restored.get("demo", "baseline") == 3
        finally:
            assert restored.close()
    finally:
        assert store.close()
    path.unlink()
    assert not path.exists()


def test_boot_identity_is_stable_for_current_system():
    from notmyfault.platform.platform_support import boot_id

    identity = boot_id()
    assert identity
    assert all(boot_id() == identity for _ in range(10))
