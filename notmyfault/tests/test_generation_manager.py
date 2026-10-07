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


def test_reload_reuses_unchanged_programs_and_rebuilds_changed_rules(monkeypatch):
    from notmyfault.core import runtime_rules

    checked = []
    validate = runtime_rules.validate_rules

    def validate_with_warning(rules, *args, **kwargs):
        checked.append(rules[0]["rule_id"])
        valid, counts, issues, warnings = validate(rules, *args, **kwargs)
        if rules[0]["rule_id"] == "r_reuse002":
            warnings.append((rules[0]["name"], "检查提示"))
        return valid, counts, issues, warnings

    monkeypatch.setattr(runtime_rules, "validate_rules", validate_with_warning)
    document = [rule("r_reuse001"), rule("r_reuse002")]
    engine = create_test_engine({"rules": []})
    engine.triggers_meta["demo"] = {"trigger_api": "event-v2", "outputs": []}
    engine.triggers_funcs["demo"] = lambda *args: None
    engine.actions_meta["noop"] = {}
    engine.actions_funcs["noop"] = lambda *args: None
    for source in document:
        source["condition"]["binding_id"] = "t_" + source["rule_id"][2:]
        source["actions"] = [{"type": "noop", "params": {}, "binding_id": "a_" + source["rule_id"][2:]}]
    try:
        old = engine._generations.apply(document, "test", activate=False)
        document[0]["concurrency"] = {"mode": "queue", "queue_limit": 2}
        new = engine._generations.apply(document, "test", activate=False)
        assert new.snapshot.compiled["r_reuse002"] is old.snapshot.compiled["r_reuse002"]
        assert new.snapshot.contexts["r_reuse002"] is old.snapshot.contexts["r_reuse002"]
        assert new.snapshot.registry is old.snapshot.registry
        assert new.snapshot.compiled["r_reuse001"] is not old.snapshot.compiled["r_reuse001"]
        assert new.snapshot.warnings == old.snapshot.warnings
        assert checked == ["r_reuse001", "r_reuse002", "r_reuse001"]
        engine.triggers_meta["demo"]["outputs"] = [{"name": "token", "type": "text", "sensitive": True}]
        changed = engine._generations.apply(document, "test", activate=False)
        assert changed.snapshot.registry is not new.snapshot.registry
        assert all(changed.snapshot.compiled[key] is not value for key, value in new.snapshot.compiled.items())
    finally:
        engine.shutdown()


def test_validated_reload_does_not_reuse_unvalidated_rules():
    source = rule()
    engine = create_test_engine({"rules": []})
    engine.triggers_meta["demo"] = {"trigger_api": "event-v2", "outputs": []}
    engine.triggers_funcs["demo"] = lambda *args: None
    source["condition"]["binding_id"] = "t_compile1"
    source["actions"] = [{"type": "noop", "params": {}, "binding_id": "a_compile1"}]
    engine.actions_meta["noop"] = {}
    engine.actions_funcs["noop"] = lambda *args: None
    try:
        old = engine._generations.apply([source], "test", activate=False, validate=False)
        new = engine._generations.apply([source], "test", activate=False)
        assert new.snapshot.validated
        assert new.snapshot.compiled[source["rule_id"]] is not old.snapshot.compiled[source["rule_id"]]
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


@pytest.mark.parametrize("replace", [False, True])
def test_watcher_detects_content_change_with_preserved_mtime_and_size(tmp_path, replace):
    import os

    path = tmp_path / "rules.json"
    path.write_text('[1]')
    original = path.stat()
    clock = FakeClock()
    calls = []
    watcher = RuleWatcher(lambda: path, path.read_text, lambda doc, source: calls.append(doc), clock=clock)
    watcher.acknowledge()
    target = tmp_path / "replacement.json" if replace else path
    target.write_text('[2]')
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    if replace:
        os.replace(target, path)
    watcher.check_once()
    clock.advance(1)
    watcher.check_once()
    clock.advance(0.3)
    watcher.check_once()
    assert calls == ['[2]']


def test_watcher_periodically_rechecks_content_when_stat_fields_are_unchanged(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "rules.json"
    path.write_text('[1]')
    original = path.stat()
    clock = FakeClock()
    calls = []
    watcher = RuleWatcher(lambda: path, path.read_text, lambda doc, source: calls.append(doc), clock=clock)
    watcher.acknowledge()
    path.write_text('[2]')
    monkeypatch.setattr(Path, "stat", lambda self, **kwargs: original)
    assert not watcher.check_once()
    clock.advance(1)
    assert not watcher.check_once()
    clock.advance(0.3)
    assert watcher.check_once()
    assert calls == ['[2]']
