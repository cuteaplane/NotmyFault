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


def test_filters_outcome_and_skips_duplicate_run_id():
    meta, module = load_trigger()
    events = []
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
        assert [item["status"] for item in events] == ["failed"]
        assert events[0]["run_id"] == "run_2"
    finally:
        stop.set()
        thread.join(timeout=2)


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
