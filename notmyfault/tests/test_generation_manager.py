import copy
import threading

import pytest

from notmyfault.core.clock import FakeClock
from notmyfault.core.rule_watcher import RuleWatcher
from notmyfault.tests.api_support import create_test_engine


def rule(identity="r_compile1", trigger="demo"):
    return {"rule_id": identity, "name": identity, "condition": {"type": trigger, "params": {}}, "actions": []}


def test_compiler_requires_existing_unique_ids_and_freezes_sources():
    engine = create_test_engine({"rules": []})
    try:
        source = rule()
        snapshot = engine._generations.prepare([source], validate=False)
        source["name"] = "外部修改"
        assert snapshot.rules[0]["name"] == "r_compile1"
        with pytest.raises(TypeError):
            snapshot.rules[0]["condition"]["params"]["x"] = 1
        with pytest.raises(ValueError, match="rule_id"):
            engine._generations.prepare([{**source, "rule_id": None}], validate=False)
        with pytest.raises(ValueError, match="重复"):
            engine._generations.prepare([source, copy.deepcopy(source)], validate=False)
    finally:
        engine.shutdown()


def test_dispatch_visits_only_subscribers():
    engine = create_test_engine({"rules": [rule(f"r_compile{i:03d}", "demo" if i == 0 else "other") for i in range(100)]})
    try:
        snapshot = engine._get_runtime_snapshot()
        calls = []
        match = snapshot.runtime.match_and_take
        snapshot.runtime.match_and_take = lambda identity, *args, **kwargs: (calls.append(identity), match(identity, *args, **kwargs))[1]
        engine.emit_event("demo", {})
        assert calls == ["r_compile000"]
        assert engine._run_manager.wait_for_idle(2)
    finally:
        engine.shutdown()


def test_running_workflow_uses_old_plugin_snapshot_after_swap():
    entered, release = threading.Event(), threading.Event()
    values = []
    source = rule()
    source["actions"] = [{"type": "wait", "params": {}}, {"type": "version", "params": {}}]
    engine = create_test_engine({"rules": [source]})
    engine.actions_meta.update(wait={}, version={"version": "1"})
    engine.actions_funcs["wait"] = lambda *_: (entered.set(), release.wait(3)) and None
    engine.actions_funcs["version"] = lambda *_: values.append("old")
    try:
        engine.emit_event("demo", {})
        assert entered.wait(2)
        old = engine._generations.current
        engine.actions_meta["version"] = {"version": "2"}
        engine.actions_funcs["version"] = lambda *_: values.append("new")
        candidate = engine._generations.apply(source and engine.rules, "test", activate=False, validate=False)
        assert candidate.snapshot.compiled[source["rule_id"]].digest != old.snapshot.compiled[source["rule_id"]].digest
        release.set()
        assert engine._run_manager.wait_for_idle(2)
        assert values == ["old"]
        engine.emit_event("demo", {})
        assert engine._run_manager.wait_for_idle(2)
        assert values == ["old", "new"]
    finally:
        release.set()
        engine.shutdown()


def test_watcher_debounces_latest_write_and_skips_identical_content(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text("[]")
    clock = FakeClock()
    calls = []
    watcher = RuleWatcher(lambda: path, lambda: path.read_text(), lambda doc, source: calls.append((doc, source)), clock=clock)
    watcher.acknowledge()
    path.write_text('[1]')
    assert not watcher.check_once()
    clock.advance(0.2)
    path.write_text('[2]')
    assert not watcher.check_once()
    clock.advance(0.299)
    assert not watcher.check_once()
    clock.advance(0.002)
    assert watcher.check_once()
    assert calls == [('[2]', 'file')]
    path.write_text('[2]')
    watcher.check_once()
    clock.advance(0.3)
    assert not watcher.check_once()
    assert len(calls) == 1


def test_watcher_debounces_deleted_file_and_deduplicates_read_error(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text("[]")
    clock = FakeClock()
    errors = []
    watcher = RuleWatcher(lambda: path, lambda: path.read_text(), lambda *args: None, clock=clock, on_error=lambda error, stage: errors.append(stage))
    watcher.acknowledge()
    path.unlink()
    assert not watcher.check_once()
    assert not errors
    clock.advance(0.3)
    assert not watcher.check_once()
    assert errors == ["read"]
    watcher.check_once()
    assert errors == ["read"]


def test_file_watcher_detects_backwards_timestamp(tmp_path):
    import os

    path = tmp_path / "rules.json"
    path.write_text("old")
    clock = FakeClock()
    calls = []
    watcher = RuleWatcher(lambda: path, path.read_text, lambda doc, source: calls.append(doc), clock=clock)
    watcher.acknowledge()
    previous = path.stat().st_mtime_ns
    path.write_text("new")
    os.utime(path, ns=(previous - 1000000000, previous - 1000000000))
    assert not watcher.check_once()
    clock.advance(0.3)
    assert watcher.check_once()
    assert calls == ["new"]


def test_watcher_start_keeps_stamp_captured_before_initial_rule_load(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text("old")
    clock = FakeClock()
    calls = []
    watcher = RuleWatcher(lambda: path, path.read_text, lambda doc, source: calls.append(doc), clock=clock)
    watcher.acknowledge()
    original = watcher._stamp
    path.write_text("new")
    try:
        watcher.start()
        assert watcher._stamp == original
        watcher.check_once()
        clock.advance(0.3)
        watcher.check_once()
        assert calls == ["new"]
    finally:
        assert watcher.stop(1)
