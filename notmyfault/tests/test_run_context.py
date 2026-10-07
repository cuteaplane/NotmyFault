import copy
import types

import pytest

from notmyfault.core.bindings import resolve_value
from notmyfault.core.run_context import RunState, referenced_context
from notmyfault.core.runtime_rules import prepare_rules
from notmyfault.core.workflow import build_context
from notmyfault.plugin_api import Blob, RunContext, Secret
from notmyfault.tests.api_support import create_test_engine
from notmyfault.values import BLOB_THRESHOLD, allow_secrets
from notmyfault.values import hold_value
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.data_types import DataTypeError


def test_run_state_separates_private_keys_from_public_view():
    state = build_context("规则", "signal", {"data": [1]}, [], "r_readonly1", "run_readonly1")
    state["_type_registry"] = object()
    state["manual_test"] = {"assertions": []}
    assert isinstance(state, RunState)
    assert "_type_registry" not in state.public
    assert "manual_test" in state.private
    view = state.view(None)
    assert isinstance(view, RunContext)
    assert set(view) == {"rule", "run", "event", "triggers", "steps", "runtime"}
    with pytest.raises(TypeError):
        view["rule"]["id"] = "changed"
    with pytest.raises(TypeError):
        view["event"]["payload"]["data"].append(2)


@pytest.mark.parametrize("secret", [False, True])
def test_readonly_container_references_reuse_validated_values(secret, monkeypatch):
    rows = hold_value([{ "value": number} for number in range(1000)])
    value = Secret(rows) if secret else rows
    registry = TypeRegistry()
    state = {"steps": {"a_source01": {"status": "ok", "result": {"rows": value}}}, "_type_registry": registry}
    reference = {"$ref": {"scope": "step", "node": "a_source01", "path": ["rows"]}}
    assert resolve_value(reference, state) is value
    monkeypatch.setattr("notmyfault.core.type_registry.is_custom_type", lambda *args: pytest.fail("重复检查只读结果"))
    assert resolve_value(reference, state) is value


def test_readonly_container_reference_rejects_nested_private_data():
    registry = TypeRegistry({"com.test.data/private@1": {"binding": "private", "schema": "any"}})
    value = hold_value({"nested": [{"$type": "com.test.data/private@1", "data": "private"}]})
    with pytest.raises(DataTypeError, match="私有"):
        registry.binding_value(value)


def test_plugin_cannot_change_prior_steps_or_executor_state():
    engine = create_test_engine({"rules": []})
    observed = []

    def produce(meta, params):
        return {"rows": [{"value": 1}]}

    def consume(meta, params, context):
        assert isinstance(context, RunContext)
        assert not any(key.startswith("_") for key in context)
        assert "manual_test" not in context
        rows = context["steps"]["a_produce1"]["result"]["rows"]
        with pytest.raises(TypeError):
            rows[0]["value"] = 2
        copied = [dict(item) for item in rows]
        copied[0]["value"] = 2
        observed.append((rows[0]["value"], copied[0]["value"]))
        return None

    engine.actions_meta.update({"produce": {}, "consume": {"execution_api": "context-v1"}})
    engine.actions_funcs.update({"produce": produce, "consume": lambda *args: None})
    engine._plugin_modules["consume"] = types.SimpleNamespace(run_with_context=consume)
    state = build_context("规则", "signal", {}, [])
    try:
        engine.execute_workflow("rule", {"actions": [
            {"type": "produce", "binding_id": "a_produce1", "params": {}},
            {"type": "consume", "binding_id": "a_consume1", "params": {}},
        ]}, "规则", state)
        assert observed == [(1, 2)]
    finally:
        engine.shutdown(2)


@pytest.mark.parametrize("size", [1, 5, 10])
def test_large_step_result_and_references_share_a_blob(size):
    engine = create_test_engine({"rules": []})
    observed = []
    result = {"body": b"x" * (size * 1024 * 1024)}
    engine.actions_meta.update({"produce": {}, "consume": {"execution_api": "context-v1"}})
    engine.actions_funcs.update({"produce": lambda *args: result, "consume": lambda *args: None})

    def consume(meta, params, context):
        blob = params["body"]
        assert isinstance(blob, Blob)
        assert context["steps"]["a_produce1"]["result"]["body"] is blob
        assert copy.deepcopy(blob) is blob
        observed.append(blob)
        return {"size": blob.size}

    engine._plugin_modules["consume"] = types.SimpleNamespace(run_with_context=consume)
    state = build_context("规则", "signal", {}, [])
    ref = {"$ref": {"scope": "step", "node": "a_produce1", "path": ["body"]}}
    try:
        engine.execute_workflow("rule", {"actions": [
            {"type": "produce", "binding_id": "a_produce1", "params": {}},
            *[{"type": "consume", "binding_id": f"a_consume{i}", "params": {"body": ref}} for i in range(10)],
        ]}, "规则", state)
        assert len(observed) == 10
        assert all(blob is observed[0] for blob in observed)
        assert resolve_value(ref, state) is observed[0]
    finally:
        engine.shutdown(2)


def test_isolated_snapshot_contains_only_referenced_sources():
    state = build_context("规则", "signal", {}, [])
    state["steps"].update({"a_used0001": {"status": "ok", "result": {"value": 7}},
                           "a_unused01": {"status": "ok", "result": {"body": Blob(b"x" * BLOB_THRESHOLD)}}})
    state["triggers"].update({"t_used0001": {"payload": {"value": 9}}, "t_unused01": {"payload": {"value": 10}}})
    snapshot = referenced_context(state, {"step": {"$ref": {"scope": "step", "node": "a_used0001", "path": ["value"]}},
                                        "trigger": {"$ref": {"scope": "trigger", "node": "t_used0001", "path": ["value"]}}})
    assert set(snapshot["steps"]) == {"a_used0001"}
    assert set(snapshot["triggers"]) == {"t_used0001"}
    assert "manual_test" not in snapshot


def test_compiler_records_snapshot_sources_for_each_step(monkeypatch):
    rule = {"rule_id": "r_sources01", "name": "规则", "condition": {"type": "signal", "params": {}}, "actions": [
        {"type": "consume", "binding_id": "a_consume1", "params": {
            "value": {"$ref": {"scope": "step", "node": "a_used0001", "path": ["value"]}},
            "legacy": "{{ triggers.t_used0001.payload.value }}",
            "literal": {"$literal": "{{ steps.a_unused01.result }}"},
        }},
    ]}
    compiled = prepare_rules([rule], {"signal": {}}, {"consume": {}}, validate=False).compiled["r_sources01"]
    assert compiled.plan[0].context_sources == (("steps", "a_used0001"), ("triggers", "t_used0001"))
    state = build_context("规则", "signal", {}, [])
    state["steps"]["a_used0001"] = {"result": {"value": 7}}
    state["triggers"]["t_used0001"] = {"payload": {"value": 9}}
    monkeypatch.setattr("notmyfault.core.run_context.context_sources", lambda value: pytest.fail("运行时重复扫描引用"))
    snapshot = referenced_context(state, {}, sources=compiled.plan[0].context_sources)
    assert snapshot["steps"]["a_used0001"]["result"]["value"] == 7
    assert snapshot["triggers"]["t_used0001"]["payload"]["value"] == 9


def test_secret_reveal_is_limited_to_current_action_parameters():
    exposed, unrelated = Secret("token-a"), Secret("token-b")
    with pytest.raises(PermissionError):
        exposed.reveal()
    with allow_secrets({"token": exposed}):
        assert exposed.reveal() == "token-a"
        with pytest.raises(PermissionError):
            unrelated.reveal()
    with pytest.raises(PermissionError):
        exposed.reveal()


def test_blob_path_is_lazy_and_holds_exact_bytes():
    blob = Blob("甲\x00乙")
    assert blob._path is None
    assert blob.size == len("甲\x00乙".encode("utf-8"))
    path = blob.as_path()
    assert path.read_bytes() == bytes(blob)
    assert blob.as_path() == path
    path.unlink()
