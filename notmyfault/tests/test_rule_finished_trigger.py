"""rule_finished 触发器：监视规则结束并发出事件"""

import importlib.util
import json
import threading
from pathlib import Path

import pytest

from notmyfault.core import engine_events
from notmyfault.security.plugin_schema import check_payload_contract, validate_plugin_meta

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "triggers" / "rule_finished"


def setup_function():
    engine_events.clear()


def teardown_function():
    engine_events.clear()


def load_trigger():
    meta = json.loads((PLUGIN_ROOT / "trigger.json").read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location(
        "rule_finished_under_test", PLUGIN_ROOT / "trigger.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return meta, module


def test_manifest_is_valid():
    meta = json.loads((PLUGIN_ROOT / "trigger.json").read_text(encoding="utf-8"))
    ok, errors = validate_plugin_meta(meta, "trigger")
    assert ok, errors


def test_emits_when_watched_rule_finishes():
    meta, module = load_trigger()
    events = []
    stop = threading.Event()
    thread = threading.Thread(
        target=module.run,
        args=(
            meta,
            {
                "rule_id": "rule_target",
                "outcome": "any",
                "include_self": False,
                "owner_rule_id": "rule_listener",
            },
            events.append,
            stop,
        ),
        daemon=True,
    )
    thread.start()
    try:
        for _ in range(50):
            if engine_events.subscriber_count() >= 1:
                break
            stop.wait(0.01)
        assert engine_events.subscriber_count() >= 1
        engine_events.publish(
            "workflow_completed",
            {
                "run_id": "run_ok",
                "rule_id": "rule_target",
                "rule_name": "目标规则",
                "status": "succeeded",
            },
        )
        engine_events.publish(
            "workflow_completed",
            {
                "run_id": "run_other",
                "rule_id": "rule_other",
                "rule_name": "其他规则",
                "status": "succeeded",
            },
        )
        assert len(events) == 1
        assert events[0]["rule_id"] == "rule_target"
        assert events[0]["status"] == "succeeded"
        assert events[0]["run_id"] == "run_ok"
        assert check_payload_contract(meta["outputs"], events[0]) == []
    finally:
        stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("reject_first", [False, True])
def test_filters_outcome_and_skips_duplicate_run_id(reject_first):
    meta, module = load_trigger()
    events = []
    attempts = []

    def receive(payload):
        attempts.append(payload)
        if reject_first and len(attempts) == 1:
            raise RuntimeError("接收失败")
        events.append(payload)

    stop = threading.Event()
    thread = threading.Thread(
        target=module.run,
        args=(
            meta,
            {
                "rule_id": "rule_target",
                "outcome": "failed",
                "include_self": False,
                "owner_rule_id": "rule_listener",
            },
            receive,
            stop,
        ),
        daemon=True,
    )
    thread.start()
    try:
        for _ in range(50):
            if engine_events.subscriber_count() >= 1:
                break
            stop.wait(0.01)
        assert engine_events.subscriber_count() == 1
        engine_events.publish(
            "workflow_completed",
            {
                "run_id": "run_1",
                "rule_id": "rule_target",
                "rule_name": "目标",
                "status": "succeeded",
            },
        )
        engine_events.publish(
            "workflow_failed",
            {"run_id": "run_2", "rule_id": "rule_target", "rule_name": "目标"},
        )
        engine_events.publish(
            "workflow_failed",
            {"run_id": "run_2", "rule_id": "rule_target", "rule_name": "目标"},
        )
        engine_events.publish(
            "workflow_failed",
            {"run_id": "run_2", "rule_id": "rule_target", "rule_name": "目标"},
        )
        assert [item["status"] for item in events] == ["failed"]
        assert events[0]["run_id"] == "run_2"
        assert len(attempts) == 1 + int(reject_first)
    finally:
        stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_exclude_self_skips_owner_rule_finish():
    meta, module = load_trigger()
    events = []
    stop = threading.Event()
    thread = threading.Thread(
        target=module.run,
        args=(
            meta,
            {
                "rule_id": "rule_self",
                "outcome": "any",
                "include_self": False,
                "owner_rule_id": "rule_self",
            },
            events.append,
            stop,
        ),
        daemon=True,
    )
    thread.start()
    try:
        for _ in range(50):
            if engine_events.subscriber_count() >= 1:
                break
            stop.wait(0.01)
        engine_events.publish(
            "workflow_completed",
            {
                "run_id": "run_self",
                "rule_id": "rule_self",
                "rule_name": "自己",
                "status": "succeeded",
            },
        )
        assert events == []
    finally:
        stop.set()
        thread.join(timeout=2)


def test_include_self_allows_owner_rule_finish():
    meta, module = load_trigger()
    events = []
    stop = threading.Event()
    thread = threading.Thread(
        target=module.run,
        args=(
            meta,
            {
                "rule_id": "rule_self",
                "outcome": "any",
                "include_self": True,
                "owner_rule_id": "rule_self",
            },
            events.append,
            stop,
        ),
        daemon=True,
    )
    thread.start()
    try:
        for _ in range(50):
            if engine_events.subscriber_count() >= 1:
                break
            stop.wait(0.01)
        engine_events.publish(
            "workflow_completed",
            {
                "run_id": "run_self",
                "rule_id": "rule_self",
                "rule_name": "自己",
                "status": "cancelled",
            },
        )
        assert len(events) == 1
        assert events[0]["status"] == "cancelled"
    finally:
        stop.set()
        thread.join(timeout=2)


def test_missing_rule_id_raises():
    meta, module = load_trigger()
    with pytest.raises(ValueError, match="未选择"):
        module.RuleFinishedTrigger(
            meta, {"rule_id": ""}, lambda payload: None, threading.Event()
        ).validate()


@pytest.mark.parametrize("include_self", ["default", False, True, "false", "true", "", 0, 1, None, [], {}])
@pytest.mark.parametrize("binding_mode", ["literal", "constant"])
def test_prepared_rule_suppresses_its_own_finish(monkeypatch, include_self, binding_mode):
    """规则自己没填 owner_rule_id 时要由引擎补上，否则运行结束会再次触发自己"""
    from notmyfault.core.rules import aggregate_trigger_params
    from notmyfault.core.runtime_rules import prepare_rules

    meta, module = load_trigger()
    params = {"rule_id": "r_self0001"}
    if include_self != "default":
        params["include_self"] = include_self
    rule = {
        "name": "监视自己",
        "rule_id": "r_self0001",
        "event": {"type": "rule_finished", "params": params},
        "actions": [{"type": "noop", "params": {}}],
    }
    if binding_mode == "constant" and include_self != "default":
        rule["constants"] = [{"id": "c_switch01", "name": "自身监视开关", "value_type": "any", "value": include_self}]
        params["include_self"] = {"$ref": {"scope": "constant", "node": "c_switch01", "path": []}}
    prepared = prepare_rules(
        [rule],
        {"rule_finished": meta},
        {"noop": {"params": []}},
    )
    if binding_mode == "literal" and include_self != "default" and not isinstance(include_self, bool):
        assert len(prepared) == 0
        assert any("布尔" in issue for _name, issue in prepared.issues)
        assert engine_events.subscriber_count() == 0
        return
    assert len(prepared) == 1, prepared.issues
    config = aggregate_trigger_params(prepared.rules)["rule_finished"][0]

    events = []
    stop = threading.Event()
    trigger = module.RuleFinishedTrigger(meta, config, events.append, stop)
    if include_self != "default" and not isinstance(include_self, bool):
        monkeypatch.setattr(trigger._api, "subscribe", lambda callback: pytest.fail("非法开关不能订阅引擎事件"))
        stop.set()
        with pytest.raises(ValueError, match="布尔"):
            trigger.run()
        assert engine_events.subscriber_count() == 0
        assert events == []
        return

    thread = threading.Thread(target=trigger.run, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            if engine_events.subscriber_count() == 1:
                break
            stop.wait(0.01)
        assert engine_events.subscriber_count() == 1
        assert trigger.owner_rule_id == "r_self0001"
        payload = {"run_id": "run_self", "rule_id": "r_self0001", "status": "succeeded"}
        for _ in range(2):
            engine_events.publish("workflow_completed", payload)
        assert len(events) == int(include_self is True)
        if events:
            assert events[0]["rule_id"] == "r_self0001"
            assert events[0]["run_id"] == "run_self"
            assert events[0]["status"] == "succeeded"
    finally:
        stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert engine_events.subscriber_count() == 0


@pytest.mark.parametrize("other_runs", [0, 256])
def test_duplicate_notifications_during_delivery(other_runs):
    meta, module = load_trigger()
    events = []
    attempts = []
    errors = []
    entered = threading.Event()
    release = threading.Event()
    stop = threading.Event()
    payload = {"run_id": "run_pending", "rule_id": "rule_target"}

    def receive(item):
        attempts.append(item["run_id"])
        if item["run_id"] == "run_pending" and attempts.count("run_pending") == 1:
            engine_events.publish("workflow_failed", payload)
            entered.set()
            if not release.wait(5):
                errors.append("接收未恢复")
                raise RuntimeError(errors[-1])
        events.append(item)

    trigger = module.RuleFinishedTrigger(meta, {"rule_id": "rule_target"}, receive, stop)
    thread = threading.Thread(target=trigger.run, daemon=True)
    publisher = threading.Thread(
        target=engine_events.publish, args=("workflow_failed", payload), daemon=True
    )
    thread.start()
    try:
        for _ in range(50):
            if engine_events.subscriber_count() == 1:
                break
            stop.wait(0.01)
        assert engine_events.subscriber_count() == 1
        publisher.start()
        assert entered.wait(2)
        for index in range(other_runs):
            engine_events.publish(
                "workflow_failed", {"run_id": f"run_other_{index}", "rule_id": "rule_target"}
            )
        engine_events.publish("workflow_failed", payload)
        assert attempts.count("run_pending") == 1
        assert len(events) == other_runs
        release.set()
        publisher.join(timeout=2)
        assert not publisher.is_alive()
        engine_events.publish("workflow_failed", payload)
        assert attempts.count("run_pending") == 1
        assert len(events) == other_runs + 1
        assert errors == []
    finally:
        release.set()
        stop.set()
        if publisher.ident is not None:
            publisher.join(timeout=2)
            assert not publisher.is_alive()
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert engine_events.subscriber_count() == 0
