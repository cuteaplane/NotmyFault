import json
from types import SimpleNamespace

import pytest

from notmyfault.core.bindings import resolve_value
from notmyfault.core.data_types import normalize_fields
from notmyfault.core.event_stream import EventStream
from notmyfault.core.value_codec import encode_value, decode_value
from notmyfault.values import Blob, FrozenList, Secret, allow_secrets, contains_secret, hold_value, plain_value
from notmyfault.core.workflow import build_context, invoke_action
from notmyfault.tests.api_support import create_test_engine
from notmyfault.tests.canvas_fixtures import trigger_rule, set_rule_steps
from notmyfault.core.run_history import RunHistory
from notmyfault.host.api.events import EventBroker


def test_short_secret_does_not_redact_unrelated_message_text():
    stream = EventStream()
    packet = stream.publish("run.step.succeeded", {"result": {"hidden": Secret("1"), "normal": "101 files"}})
    assert packet.to_dict()["data"]["result"] == {"hidden": "***", "normal": "101 files"}
    assert str(Secret("token")) == "***"
    assert json.dumps(Secret("token")) == '"***"'
    assert "token" not in repr(Secret("token"))
    assert json.dumps(encode_value({"token": Secret("token")})) == '{"token": "***"}'
    stream.close()


@pytest.mark.parametrize("sensitive", [False, True])
def test_frozen_parameter_permissions_and_events_reuse_prepared_data(monkeypatch, sensitive):
    secret = Secret("nested-token")
    value = hold_value([secret if sensitive else "value"] * 1000)
    stream = EventStream()
    first = stream.publish("run.step.started", {"params": {"rows": value}})
    monkeypatch.setattr(FrozenList, "__iter__", lambda self: pytest.fail("重复遍历只读参数"))
    try:
        assert contains_secret(value) is sensitive
        with allow_secrets({"rows": value}):
            if sensitive:
                assert secret.reveal() == "nested-token"
            else:
                with pytest.raises(PermissionError):
                    secret.reveal()
        second = stream.publish("run.step.started", {"params": {"rows": value}})
        assert second.data["params"]["rows"] is first.data["params"]["rows"]
    finally:
        stream.close()


def test_frozen_secret_markers_cannot_be_changed():
    value = hold_value([Secret("token")])
    with pytest.raises(TypeError):
        value._secret_ids = frozenset()


def test_event_freeze_cache_releases_source_after_public_event_is_saved():
    import gc
    import weakref
    from notmyfault.core import event_stream

    stream = EventStream()
    value = hold_value([Secret("cached-token")])
    identity = id(value)
    reference = weakref.ref(value)
    packet = stream.publish("run.step.started", {"params": {"rows": value}})
    assert identity in event_stream._frozen_values
    del value
    gc.collect()
    assert reference() is None
    assert identity not in event_stream._frozen_values
    assert packet.to_dict()["data"]["params"]["rows"] == ["***"]
    stream.close()


@pytest.mark.parametrize("expression", [
    {"$template": ["prefix/", {"$ref": {"scope": "constant", "node": "c_token001", "path": []}}, "/suffix"]},
    "prefix/{{ constants.c_token001 }}/suffix",
])
def test_templates_keep_derived_text_secret(expression):
    value = resolve_value(expression, {"constants": {"c_token001": Secret("token")}})
    if isinstance(expression, str):
        assert value == expression
        assert not isinstance(value, Secret)
        return
    assert isinstance(value, Secret)
    assert encode_value(value) == "***"
    with allow_secrets({"value": value}):
        assert value.reveal() == "prefix/token/suffix"


def test_field_normalization_protects_sensitive_nested_values():
    result = normalize_fields({"data": {"token": "value"}}, [{"name": "data", "type": "any", "sensitive": True}], wrap_sensitive=True)
    assert isinstance(result["data"], Secret)
    value = resolve_value({"$ref": {"scope": "step", "node": "a_source01", "path": ["data", "token"]}},
                          {"steps": {"a_source01": {"status": "ok", "result": result}}})
    assert isinstance(value, Secret)
    assert plain_value(value) == "value"


def test_public_codec_redacts_but_worker_codec_round_trips_wrappers():
    source = {"hidden": Secret("token"), "body": Blob(b"data")}
    public = encode_value(source)
    assert public["hidden"] == "***"
    assert public["body"] == source["body"].descriptor()
    transferred = decode_value(encode_value(source, _transport=True), _transport=True)
    assert isinstance(transferred["hidden"], Secret)
    assert isinstance(transferred["body"], Blob)
    assert bytes(transferred["body"]) == b"data"
    with pytest.raises(ValueError):
        decode_value(encode_value(source, _transport=True))


def test_action_can_reveal_only_declared_parameters_and_protects_derived_output():
    declared, undeclared, unrelated = Secret("declared-token"), Secret("undeclared-token"), Secret("context-token")
    state = build_context("规则", "signal", {"token": unrelated}, [])

    def run(meta, params, context):
        with pytest.raises(PermissionError):
            params["extra"].reveal()
        with pytest.raises(PermissionError):
            context["event"]["payload"]["token"].reveal()
        return {"derived": params["token"].reveal() + "/result"}

    result = invoke_action(lambda *args: None, SimpleNamespace(run_with_context=run),
                           {"execution_api": "context-v1", "params": [{"name": "token", "type": "string"}]},
                           {"token": declared, "extra": undeclared}, state)
    assert isinstance(result, Secret)
    assert plain_value(result) == {"derived": "declared-token/result"}
    with pytest.raises(PermissionError):
        declared.reveal()


def test_event_boundary_uses_snapshot_registry_for_sensitive_custom_output():
    identity = "com.test.secret/record@1"
    rule = set_rule_steps(trigger_rule("r_custom01", "规则", "record_event"), [
        {"type": "consume", "params": {"name": {"$ref": {"scope": "event", "path": ["record", "data", "name"]}}}},
    ])
    engine = create_test_engine({"rules": [rule]})
    observed, packets = [], []
    engine.triggers_meta["record_event"] = {
        "package_name": "com.test.secret", "contributes": {"data_types": [{
            "id": "record", "version": 1, "binding": "shared", "schema": {"type": "object", "properties": {"name": "text"}},
        }]}, "outputs": [{"name": "record", "type": identity, "sensitive": True}],
    }
    engine.actions_meta["consume"] = {"params": [{"name": "name", "type": "string", "value_type": "text"}]}
    engine.actions_funcs["consume"] = lambda meta, params, _run_context: observed.append(params["name"])
    engine.events.subscribe(packets.append)
    try:
        engine.emit_event("record_event", {"record": {"$type": identity, "data": {"name": "甲"}}})
        assert engine._run_manager.wait_for_idle(2)
        assert engine.events.flush()
        assert observed == ["甲"]
        event = next(packet.to_dict() for packet in packets if packet.type == "run.triggered")
        assert event["data"]["event_payload"]["record"] == "***"
    finally:
        engine.shutdown(2)


@pytest.mark.parametrize("value,path,operator,expected", [
    (Secret({"token": "abc-token"}), ["token"], "contains", "token"),
    ({"token": Secret(7)}, ["token"], "gt", 5),
    ({"body": Blob("abc-token")}, ["body"], "contains", "token"),
])
def test_manual_checks_read_wrapped_values_without_publishing_them(value, path, operator, expected):
    engine = create_test_engine({"rules": []})
    state = build_context("规则", "manual", {}, [])
    state["steps"]["a_source01"] = {"status": "ok", "result": value}
    state["manual_test"] = {"assertions": [{"step_id": "a_source01", "path": path, "operator": operator, "expected": expected}]}
    packets = []
    engine.events.subscribe(packets.append)
    try:
        summary = engine._workflow_executor.evaluate_test_assertions(state, "规则")
        assert summary["passed"] == summary["total"] == 1
        assert engine.events.flush()
        assert "abc-token" not in json.dumps([packet.to_dict() for packet in packets])
    finally:
        engine.shutdown(2)


def test_manual_continuation_wraps_provided_sensitive_outputs_without_requiring_other_fields():
    rule = set_rule_steps(trigger_rule("r_manual_secret", "规则", "signal"), [
        {"type": "source", "binding_id": "a_source01", "params": {}},
        {"type": "consume", "binding_id": "a_consume1", "params": {}},
    ])
    engine = create_test_engine({"rules": []})
    observed = []
    engine.triggers_meta["signal"] = {"outputs": [{"name": "required", "type": "text"}]}
    engine.actions_meta["source"] = {"outputs": [{"name": "token", "type": "text", "sensitive": True},
                                                  {"name": "other", "type": "int"}]}
    engine.actions_meta["consume"] = {"execution_api": "context-v1"}

    def consume(meta, params, context):
        value = context["steps"]["a_source01"]["result"]["token"]
        assert isinstance(value, Secret)
        with pytest.raises(PermissionError):
            value.reveal()
        observed.append(str(value))

    engine.actions_funcs.update(source=lambda *args: None, consume=lambda *args: None)
    engine._plugin_modules["consume"] = SimpleNamespace(run_with_context=consume)
    try:
        ok, _, run_id = engine.run_manual_rule_snapshot(rule, step_outputs={"a_source01": {"token": "provided-token"}},
                                                       start_step_id="a_consume1")
        assert ok
        assert engine._run_manager.wait_for_idle(2)
        assert engine._run_manager.state(run_id) == "succeeded"
        assert observed == ["***"]
    finally:
        engine.shutdown(2)


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("source", ["event", "trigger_config"])
def test_sensitive_inputs_remain_hidden_in_events_and_history(tmp_path, manual, source):
    token = "input-secret-token"
    reference = {"scope": source, "path": ["token"]}
    config = {}
    constants = []
    if source == "trigger_config":
        reference["node"] = "t_signal01"
        config = {"token": {"$ref": {"scope": "constant", "node": "c_token001", "path": []}}}
        constants = [{"id": "c_token001", "name": "口令", "value_type": "text", "value": token, "sensitive": True}]
    rule = set_rule_steps(trigger_rule("r_secret01", "规则", "signal", config, trigger_id="t_signal01"), [
        {"type": "echo", "binding_id": "a_echo0001", "params": {"value": {"$ref": reference}}},
    ])
    rule["constants"] = constants
    engine = create_test_engine({"rules": [rule]})
    engine.triggers_meta["signal"] = {"params": [{"name": "token", "type": "string"}],
                                       "outputs": [{"name": "token", "type": "text", "sensitive": True}]}
    engine.actions_meta["echo"] = {"params": [{"name": "value", "type": "string", "summary": "value"}],
                                   "outputs": [{"name": "value", "type": "text", "summary": "value"}]}
    observed, packets = [], []
    engine.actions_funcs["echo"] = lambda meta, params, _run_context: observed.append(params["value"]) or {"value": params["value"]}
    history = RunHistory(str(tmp_path / "runs.jsonl"))
    broker = EventBroker(history)
    engine.events.subscribe(broker.publish)
    engine.events.subscribe(lambda event: packets.append(event.to_dict()))
    try:
        if manual:
            ok, _, run_id = engine.run_manual_rule_snapshot(rule, event_payload={"token": token}, trigger_payloads={"t_signal01": {"token": token}})
            assert ok
        else:
            engine.emit_event("signal", {"token": token}, instance={"config": {"token": token} if config else {}})
        assert engine._run_manager.wait_for_idle(2)
        assert engine.events.flush() and history.flush()
        assert observed == [token]
        assert token not in json.dumps(packets)
        assert token not in (tmp_path / "runs.jsonl").read_text(encoding="utf-8")
    finally:
        engine.shutdown(2)


def test_manual_event_protects_sensitive_fields_from_any_candidate_trigger():
    rule = set_rule_steps(trigger_rule("r_manual_multi", "规则", "public"), [
        {"type": "echo", "params": {"value": {"$ref": {"scope": "event", "path": ["token"]}}}},
    ])
    graph = rule["workflow"]
    first, action = graph["nodes"]
    second = {"type": "trigger", "binding_id": "a_private0001", "trigger": {"type": "private", "binding_id": "t_private0001", "params": {}}}
    combination = {"type": "logic", "binding_id": "a_signals0001", "op": "any"}
    graph["nodes"] += [second, combination]
    graph["edges"] = [
        {"source": first["binding_id"], "port": "event", "target": combination["binding_id"], "channel": "condition"},
        {"source": second["binding_id"], "port": "event", "target": combination["binding_id"], "channel": "condition"},
        {"source": combination["binding_id"], "port": "result", "target": action["binding_id"], "channel": "control"},
    ]
    engine = create_test_engine({"rules": []})
    packets = []
    engine.triggers_meta.update(public={"outputs": [{"name": "token", "type": "text"}]},
                                private={"outputs": [{"name": "token", "type": "text", "sensitive": True}]})
    engine.actions_funcs["echo"] = lambda meta, params, _run_context: params["value"]
    engine.actions_meta["echo"] = {"params": [{"name": "value", "type": "string"}]}
    engine.events.subscribe(lambda event: packets.append(event.to_dict()))
    try:
        assert engine.run_manual_rule_snapshot(rule, event_payload={"token": "manual-private"})[0]
        assert engine._run_manager.wait_for_idle(2) and engine.events.flush()
        assert "manual-private" not in json.dumps(packets)
    finally:
        engine.shutdown(2)
