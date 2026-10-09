from copy import deepcopy
from types import SimpleNamespace

import pytest

from notmyfault.host.api.services.rule_runs import RuleRunService, RuleRunServiceError
from notmyfault.host.api.services.rules import RuleService
from notmyfault.host.ai_rule_draft import parse_rule_draft
from notmyfault.host.ai_skills import build_rule_drafting_skill
from notmyfault.host.ai_tools import ToolCallError
from notmyfault.security.rule_approval import changed_admin_plugins


def graph_rule():
    return {
        "rule_id": "r_graph001", "name": "嵌套组测试",
        "condition": {"binding_id": "t_start001", "type": "start", "params": {}},
        "workflow": {
            "entry": "a_source001",
            "nodes": [
                {"binding_id": "a_source001", "type": "produce", "params": {}},
                {"binding_id": "a_group001", "type": "action_group", "workflow": {
                    "entry": "a_consume01", "nodes": [{
                        "binding_id": "a_consume01", "type": "consume",
                        "params": {"message": {"$ref": {"scope": "step", "node": "a_source001", "path": ["value"]}}},
                    }], "edges": [{"source": "a_consume01", "port": "next", "target": None}],
                }},
            ],
            "edges": [
                {"source": "a_source001", "port": "next", "target": "a_group001"},
                {"source": "a_group001", "port": "next", "target": None},
            ],
        },
    }


def service_for(rule):
    calls = []
    engine = SimpleNamespace(
        triggers_meta={"start": {"outputs": []}},
        actions_meta={
            "produce": {"outputs": [{"name": "value", "type": "string", "required": True}]},
            "consume": {"params": [{"name": "message", "type": "string"}]},
        },
        run_manual_rule_snapshot=lambda *args, **kwargs: (calls.append((args, kwargs)) or (True, "已提交", "run_graph001")),
    )
    store = SimpleNamespace(load_verified_rules=lambda: [deepcopy(rule)])
    return RuleRunService(SimpleNamespace(current_engine=engine), store), calls


def test_nested_graph_test_accepts_upstream_output_and_stable_node_id():
    service, calls = service_for(graph_rule())
    result = service.run(0, {"start_step_id": "a_consume01", "step_outputs": {"a_source001": {"value": "已就绪"}}})
    assert result["run_id"] == "run_graph001"
    assert calls[0][1]["start_step_id"] == "a_consume01"
    assert calls[0][1]["step_outputs"] == {"a_source001": {"value": "已就绪"}}


def test_nested_graph_test_requires_the_skipped_parent_output():
    service, calls = service_for(graph_rule())
    with pytest.raises(RuleRunServiceError) as caught:
        service.run(0, {"start_step_id": "a_consume01"})
    assert caught.value.body["code"] == "missing_test_context"
    assert caught.value.body["required_step_ids"] == ["a_source001"]
    assert calls == []


@pytest.mark.parametrize("value, accepted", [("已就绪", True), ({"invalid": True}, False)])
def test_partial_run_validates_the_exported_group_output(value, accepted):
    rule = graph_rule()
    source, group = rule["workflow"]["nodes"]
    group["workflow"] = {"entry": source["binding_id"], "nodes": [source],
                         "edges": [{"source": source["binding_id"], "port": "next", "target": None}]}
    group["exports"] = {"value": {"$ref": {"scope": "step", "node": source["binding_id"], "path": ["value"]}}}
    consumer = {"binding_id": "a_after001", "type": "consume", "params": {
        "message": {"$ref": {"scope": "step", "node": group["binding_id"], "path": ["value"]}}}}
    rule["workflow"].update(entry=group["binding_id"], nodes=[group, consumer], edges=[
        {"source": group["binding_id"], "port": "next", "target": consumer["binding_id"]},
        {"source": consumer["binding_id"], "port": "next", "target": None}])
    service, calls = service_for(rule)
    request = {"start_step_id": consumer["binding_id"], "step_outputs": {group["binding_id"]: {"value": value}}}
    if accepted:
        assert service.run(0, request)["run_id"] == "run_graph001"
        assert calls
    else:
        with pytest.raises(RuleRunServiceError) as caught:
            service.run(0, request)
        assert caught.value.body["code"] == "invalid_test_payload"
        assert not calls


def test_graph_test_rejects_injecting_future_output():
    service, calls = service_for(graph_rule())
    with pytest.raises(RuleRunServiceError) as caught:
        service.run(0, {"start_step_id": "a_source001", "step_outputs": {"a_consume01": {"message": "无效"}}})
    assert caught.value.body["code"] == "invalid_test_payload"
    assert calls == []


def test_graph_test_rejects_an_unreachable_end():
    service, calls = service_for(graph_rule())
    with pytest.raises(RuleRunServiceError):
        service.run(0, {"start_step_id": "a_group001", "end_step_id": "a_source001"})
    assert calls == []


def test_admin_approval_scans_nested_graph_and_failure_actions():
    rule = graph_rule()
    rule["workflow"]["nodes"][1]["workflow"]["nodes"][0]["failure_actions"] = [{"type": "restricted", "params": {}}]
    schema = {"triggers": {}, "actions": {"restricted": {"security": {"rule_approval": "admin_key"}}}}
    assert changed_admin_plugins([], [rule], schema) == ["restricted"]


def test_nested_validation_issue_identifies_the_action_and_parent_group():
    rule = graph_rule()
    rule["workflow"]["nodes"][1]["workflow"]["nodes"][0]["params"]["message"]["$ref"]["node"] = "a_unknown01"
    store = SimpleNamespace(load_verified_rules=lambda **kwargs: [rule])
    service = RuleService(store, lambda: {
        "triggers": {"start": {"params": [], "outputs": []}},
        "actions": {"produce": {"params": [], "outputs": [{"name": "value", "type": "text"}]},
                    "consume": {"params": [{"name": "message", "type": "string"}]}},
    })
    result = service.validate_draft(rule)
    issue = next(item for item in result["issues"] if item["code"] == "unknown_source")
    assert issue["node_id"] == "a_consume01"
    assert issue["group_path"] == ["a_group001"]


def draft_catalog():
    return {"triggers": {"start": {"params": []}}, "actions": {
        "produce": {"params": []}, "consume": {"params": [{"name": "message"}]},
    }}


def test_ai_draft_accepts_graph_and_nested_group():
    draft = graph_rule()
    draft.pop("rule_id")
    assert parse_rule_draft(draft, draft_catalog()) == draft
    schema = build_rule_drafting_skill(draft_catalog()).tools[0].parameters
    assert "workflow" in schema["properties"]
    assert schema["$defs"]["workflow_node"]["anyOf"][0]["properties"]["type"]["enum"] == ["consume", "produce"]
    action_schema = schema["$defs"]["workflow_node"]["anyOf"][0]["properties"]
    assert action_schema["retry_backoff"]["enum"] == ["fixed", "exponential"]
    assert action_schema["retry"]["maximum"] == 3


@pytest.mark.parametrize("change", ["unknown_plugin", "unknown_field", "cycle"])
def test_ai_draft_rejects_invalid_graph(change):
    draft = graph_rule()
    draft.pop("rule_id")
    source = draft["workflow"]["nodes"][0]
    if change == "unknown_plugin":
        source["type"] = "unknown"
    elif change == "unknown_field":
        source["code"] = "unused"
    else:
        draft["workflow"]["edges"][1]["target"] = "a_source001"
    with pytest.raises(ToolCallError) as caught:
        parse_rule_draft(draft, draft_catalog())
    assert caught.value.code == "rule_invalid"
