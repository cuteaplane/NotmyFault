"""结构化绑定的静态校验、上下文构建与 guaranteed 来源推导"""

import json
from pathlib import Path

import pytest

from notmyfault.core.bindings import (
    BindingResolutionError,
    BindingSkip,
    contains_dynamic_value,
    iter_references,
    references_available,
    resolve_value,
)
from notmyfault.core.data_types import DataTypeError
from notmyfault.core.variables import assign_variable, initialize_variables, resolve_trigger_constants
from notmyfault.core.rules import guaranteed_trigger_ids, validate_rule_bindings
from notmyfault.core.workflow import build_context


TRIGGERS_META = {
    "usb_insert": {
        "outputs": [{"name": "drive", "type": "string"}],
        "params": [{"name": "drive", "type": "string"}],
    },
    "hotkey": {
        "outputs": [{"name": "keys", "type": "string"}],
    },
}

ACTIONS_META = {
    "notify": {
        "params": [{"name": "message", "type": "textarea", "label": "内容"}],
    },
    "open_url": {
        "params": [{"name": "url", "type": "string", "label": "地址"}],
        "outputs": [{"name": "status", "type": "string"}],
    },
    "plugin_document": {
        "params": [{
            "name": "document",
            "type": "plugin_data",
            "value_type": "object",
            "label": "插件文档",
        }],
    },
}


def test_plugin_data_cannot_bind_trigger_or_action_output():
    rule = _rule(
        _leaf("usb_insert", "t_usb001"),
        [{
            "type": "plugin_document",
            "binding_id": "a_doc001",
            "params": {"document": _ref("trigger", "t_usb001", ["drive"])},
        }],
    )
    issues = validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META)
    assert [issue["code"] for issue in issues] == ["private_plugin_data"]


def _ref(scope, node=None, path=None):
    ref = {"scope": scope, "path": path or []}
    if node is not None:
        ref["node"] = node
    return {"$ref": ref}


def _rule(condition, actions, preconditions=None):
    rule = {"name": "测试规则", "condition": condition, "actions": actions}
    if preconditions:
        rule["preconditions"] = preconditions
    return rule


def _leaf(event_type, binding_id, params=None):
    return {"type": event_type, "params": params or {}, "binding_id": binding_id}


def test_build_context_indexes_condition_payloads_by_binding_id():
    condition_events = [
        {
            "binding_id": "t_usb001",
            "event": {"type": "usb_insert", "params": {"drive": "E:"}},
            "payload": {"drive": "E:", "label": "U盘"},
        },
        {"binding_id": "", "event": {"type": "hotkey"}, "payload": {}},
    ]
    ctx = build_context("规则A", "usb_insert", {"drive": "E:"}, condition_events)
    assert ctx["context_version"] == 2
    assert ctx["rule"] == {"name": "规则A"}
    assert set(ctx["triggers"]) == {"t_usb001"}
    entry = ctx["triggers"]["t_usb001"]
    assert entry["type"] == "usb_insert"
    assert entry["payload"] == {"drive": "E:", "label": "U盘"}
    # v2 叶子的 params 作为触发器配置进入 config
    assert entry["config"] == {"drive": "E:"}
    assert ctx["steps"] == {}


def test_guaranteed_sources_respect_nested_any_and_all():
    tree = {
        "op": "all",
        "children": [
            _leaf("usb_insert", "t_usb001"),
            {
                "op": "any",
                "children": [
                    _leaf("hotkey", "t_hot001"),
                    {
                        "op": "all",
                        "children": [
                            _leaf("hotkey", "t_hot001"),
                            _leaf("usb_insert", "t_usb002"),
                        ],
                    },
                ],
            },
        ],
    }
    # all 取并集，any 取交集：hotkey 分支与嵌套 all 都保证 hot001 命中
    assert guaranteed_trigger_ids(tree) == {"t_usb001", "t_hot001"}


def test_actions_allow_conditional_or_source():
    rule = _rule(
        {
            "op": "any",
            "children": [_leaf("usb_insert", "t_usb001"), _leaf("hotkey", "t_hot001")],
        },
        [
            {
                "type": "notify",
                "binding_id": "a_not001",
                "params": {"message": _ref("trigger", "t_usb001", ["drive"])},
            }
        ],
    )
    # 动作允许引用 any 分支里的触发器，运行时按可用性跳过
    assert validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META) == []


def test_static_validation_accepts_guaranteed_typed_trigger_output():
    rule = _rule(
        _leaf("usb_insert", "t_usb001"),
        [
            {
                "type": "notify",
                "binding_id": "a_not001",
                "params": {"message": _ref("trigger", "t_usb001", ["drive"])},
            }
        ],
    )
    assert validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META) == []


def test_if_bindings_cannot_cross_branches_or_reference_later_actions():
    first = {"type": "open_url", "binding_id": "a_first01", "params": {"url": "https://example.com"}}
    second = {"type": "notify", "binding_id": "a_second01", "params": {"message": _ref("step", "a_first01", ["status"])}}
    branch = {"type": "if", "binding_id": "a_branch01", "condition": {"op": "eq", "left": _ref("trigger", "t_usb001", ["drive"]), "right": "D:"}, "then": [first, second], "else": []}
    rule = _rule(_leaf("usb_insert", "t_usb001"), [branch])
    assert validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META) == []
    branch["then"] = [first]
    branch["else"] = [second]
    assert [issue["code"] for issue in validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META)] == ["forward_reference"]
    branch["else"] = []
    rule["actions"].append(second)
    assert [issue["code"] for issue in validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META)] == ["forward_reference"]


def test_elif_bindings_use_only_previous_path_and_keep_unique_ids():
    from notmyfault.core.rule_model import normalize_rules
    from notmyfault.core.rules import iter_action_nodes, validate_rule_binding_ids, get_rule_admin_plugins
    from notmyfault.core.runtime_rules import prepare_rules

    source = {"type": "open_url", "binding_id": "a_source01", "params": {"url": "https://example.com"}}
    consumer = {"type": "notify", "binding_id": "a_consume1", "params": {"message": _ref("step", "a_source01", ["status"])}}
    branch = {"type": "if", "condition": {"op": "is_true", "left": False}, "then": [], "else": [],
              "elif": [{"condition": {"op": "is_true", "left": True}, "then": [source, consumer]}]}
    rule = normalize_rules([_rule(_leaf("usb_insert", "t_usb001"), [branch])])[0]
    assert validate_rule_binding_ids(rule) == []
    assert validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META) == []
    prepared = prepare_rules([rule], TRIGGERS_META, ACTIONS_META)
    assert len(prepared) == 1, prepared.issues
    assert "a_consume1" in prepared.compiled[rule["rule_id"]].step_index
    assert get_rule_admin_plugins(rule, TRIGGERS_META, {"open_url": {"permissions": ["admin"]}}) == ["open_url"]
    assert len(list(iter_action_nodes(rule["actions"]))) == 3
    rule["actions"][0]["elif"].append({"condition": {"op": "eq", "left": _ref("step", "a_source01", ["status"]), "right": "ok"}, "then": []})
    assert [issue["code"] for issue in validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META)] == ["forward_reference"]


def test_exists_accepts_optional_field_but_checks_source_path_and_comparison_type():
    meta = {"optional": {"outputs": [{"name": "state", "type": "string", "required": False}]}}
    ref = _ref("trigger", "t_option1", ["state"])
    branch = {"type": "if", "binding_id": "a_branch01", "condition": {"op": "exists", "left": ref},
              "then": [{"type": "notify", "params": {"message": "present"}}], "else": []}
    rule = _rule(_leaf("optional", "t_option1"), [branch])
    assert validate_rule_bindings(rule, meta, ACTIONS_META) == []
    ref["$ref"]["path"] = ["unknown"]
    assert [issue["code"] for issue in validate_rule_bindings(rule, meta, ACTIONS_META)] == ["unknown_output"]
    ref["$ref"]["path"] = ["state"]
    ref["$ref"]["on_missing"] = "error"
    branch["condition"] = {"op": "gt", "left": ref, "right": 1}
    assert [issue["code"] for issue in validate_rule_bindings(rule, meta, ACTIONS_META)] == ["binding_type_mismatch"]


def test_run_status_references_are_checked_across_rules_and_on_target_removal():
    from notmyfault.core.rules import normalize_rule_input, RuleStructureError, validate_rules
    from notmyfault.core.runtime_rules import prepare_rules

    rule = {"rule_id": "r_observer", **_rule(_leaf("hotkey", "t_hot001"), [{
        "type": "if", "binding_id": "a_branch01", "condition": {"op": "is_true", "left": False}, "then": [], "else": [],
        "elif": [{"condition": {"op": "run_status", "rule_id": "r_target01", "status": "succeeded"},
                  "then": [{"type": "notify", "params": {"message": "done"}}]}],
    }])}
    target = {"rule_id": "r_target01", **_rule(_leaf("hotkey", "t_target01"), [{"type": "notify", "params": {"message": "target"}}])}
    rules = normalize_rule_input([rule, target])
    assert validate_rules(rules, TRIGGERS_META, ACTIONS_META)[0] == 2
    assert len(prepare_rules(rules, TRIGGERS_META, ACTIONS_META)) == 2
    with pytest.raises(RuleStructureError, match="引用的自动化不存在"):
        normalize_rule_input([rules[0]])
    assert validate_rules([rules[0]], TRIGGERS_META, ACTIONS_META, known_rule_ids={"r_target01", "r_observer"})[0] == 1


def test_event_reference_and_legacy_template_remain_supported():
    rule = _rule(
        _leaf("usb_insert", "t_usb001"),
        [
            {
                "type": "notify",
                "binding_id": "a_not001",
                "params": {"message": _ref("event", path=["drive"])},
            },
            {
                "type": "notify",
                "binding_id": "a_not002",
                "params": {"message": "盘符 {{ event.payload.drive }}"},
            },
        ],
    )
    # event scope 与旧模板都不应产生静态问题
    assert validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META) == []


def test_file_operation_allows_dynamic_source_but_not_destination():
    manifest_path = Path(__file__).parents[1] / "actions" / "file_operation" / "action.json"
    action_meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    triggers_meta = {
        "usb_insert": {"outputs": [{"name": "actual_drive", "type": "string"}]},
    }
    actions_meta = {"file_operation": action_meta}
    rule = _rule(
        _leaf("usb_insert", "t_usb001"),
        [{
            "type": "file_operation",
            "binding_id": "a_copy001",
            "params": {
                "operation": "copy",
                "source": _ref("trigger", "t_usb001", ["actual_drive"]),
                "destination": "D:/backup",
            },
        }],
    )

    assert validate_rule_bindings(rule, triggers_meta, actions_meta) == []

    rule["actions"][0]["params"]["source"] = "C:/source"
    rule["actions"][0]["params"]["destination"] = _ref(
        "trigger", "t_usb001", ["actual_drive"]
    )
    issues = validate_rule_bindings(rule, triggers_meta, actions_meta)
    assert [issue["code"] for issue in issues] == ["unsafe_dynamic_parameter"]


@pytest.mark.parametrize(
    "value",
    [
        "{{ _run_cancel_event }}",
        {"$ref": {"scope": "event", "path": ["_private"]}},
    ],
)
def test_bindings_cannot_read_internal_context_keys(value):
    context = {
        "_run_cancel_event": "private",
        "event": {"payload": {"_private": "private"}},
    }

    with pytest.raises(BindingResolutionError, match="非法的数据路径段|运行数据不存在"):
        resolve_value(value, context)


def test_failure_actions_can_use_earlier_main_and_recovery_outputs():
    rule = _rule(
        _leaf("usb_insert", "t_usb001"),
        [
            {"type": "open_url", "binding_id": "a_early01", "params": {"url": "x"}},
            {
                "type": "open_url",
                "binding_id": "a_parent1",
                "params": {"url": "x"},
                "failure_actions": [
                    {
                        "type": "open_url",
                        "binding_id": "a_recover1",
                        "params": {"url": _ref("step", "a_early01", ["status"])},
                    },
                    {
                        "type": "notify",
                        "binding_id": "a_recover2",
                        "params": {"message": _ref("step", "a_recover1", ["status"])},
                    },
                ],
            },
        ],
    )

    assert validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META) == []


def test_failure_branch_and_main_flow_cannot_use_conditional_recovery_outputs():
    rule = _rule(
        _leaf("usb_insert", "t_usb001"),
        [
            {
                "type": "open_url",
                "binding_id": "a_parent1",
                "params": {"url": "x"},
                "failure_actions": [{
                    "type": "notify",
                    "binding_id": "a_recover1",
                    "params": {"message": _ref("step", "a_parent1", ["status"])},
                }],
            },
            {
                "type": "notify",
                "binding_id": "a_after01",
                "params": {"message": _ref("step", "a_recover1", ["status"])},
            },
        ],
    )

    codes = [issue["code"] for issue in validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META)]
    assert codes == ["forward_reference", "forward_reference"]


def test_reference_iterator_reports_nested_parameter_location():
    value = {"body": [{"$ref": {"scope": "trigger", "node": "t_usb001", "path": ["drive"]}}]}
    usages = list(iter_references(value, location="actions[0].params.payload"))
    assert len(usages) == 1
    assert usages[0].location == "actions[0].params.payload.body[0]"
    assert usages[0].reference["node"] == "t_usb001"


def test_reference_path_rejects_special_attribute_names():
    context = {"triggers": {"t_usb001": {"payload": {}}}}
    with pytest.raises(BindingResolutionError) as excinfo:
        resolve_value(
            _ref("trigger", "t_usb001", ["__class__"]),
            context,
            location="actions[0].params.message",
        )
    assert excinfo.value.code == "invalid_reference"
    assert "__class__" in str(excinfo.value)


def test_missing_reference_is_a_permanent_configuration_error():
    # 缺失数据属于配置错误，调用方通过 ValueError 识别
    context = {"triggers": {"t_usb001": {"payload": {}}}, "steps": {}}
    with pytest.raises(ValueError) as excinfo:
        resolve_value(
            _ref("trigger", "t_usb001", ["drive"]),
            context,
            location="actions[0].params.message",
        )
    assert isinstance(excinfo.value, BindingResolutionError)
    assert excinfo.value.code == "missing_binding_value"
    assert excinfo.value.as_dict()["location"] == "actions[0].params.message"


def test_structured_reference_preserves_original_types():
    context = {
        "triggers": {
            "t_usb001": {
                "payload": {"count": 5, "items": [1, 2], "ok": True},
            }
        },
        "steps": {},
    }
    resolved = resolve_value(_ref("trigger", "t_usb001", ["count"]), context)
    assert resolved == 5 and isinstance(resolved, int)
    resolved = resolve_value(_ref("trigger", "t_usb001", ["items"]), context)
    assert resolved == [1, 2]
    resolved = resolve_value(_ref("trigger", "t_usb001", ["ok"]), context)
    assert resolved is True
    resolved = resolve_value("{{ triggers.t_usb001.payload.count }}", context)
    assert resolved == 5 and isinstance(resolved, int)


def test_nested_keys_templates_and_explicit_conversion_preserve_literal_data():
    context = {"steps": {"a_source01": {"status": "ok", "result": {"记录": [{"文件.名": "report.csv", "数量": "12"}]}}}}
    source = _ref("step", "a_source01", ["记录", 0, "文件.名"])
    count = {"$convert": {"value": _ref("step", "a_source01", ["记录", 0, "数量"]), "to": "int"}}
    assert resolve_value(count, context) == 12
    assert resolve_value({"$template": ["目标/", source]}, context) == "目标/report.csv"
    with pytest.raises(BindingResolutionError):
        resolve_value({"$template": [count]}, context)
    literal = {"$literal": {"$ref": {"scope": "step", "node": "missing"}, "text": "{{ event.payload.secret }}"}}
    assert resolve_value(literal, context) == literal["$literal"]
    assert list(iter_references(literal)) == []
    assert not contains_dynamic_value(literal)
    assert contains_dynamic_value({"nested": [count]})
    assert references_available(literal, context)


def test_optional_reference_policies_do_not_hide_failed_sources_or_null_values():
    context = {"steps": {"a_source01": {"status": "ok", "result": {"null": None, "zero": 0, "flag": False}}}}
    def optional(path, policy="default", fallback="missing"):
        reference = {"scope": "step", "node": "a_source01", "path": [path], "on_missing": policy}
        if policy == "default":
            reference["default"] = fallback
        return {"$ref": reference}
    assert resolve_value(optional("absent"), context) == "missing"
    for field, expected in (("null", None), ("zero", 0), ("flag", False)):
        assert resolve_value(optional(field), context) is expected
    assert not references_available(optional("absent", "skip"), context)
    with pytest.raises(BindingSkip):
        resolve_value(optional("absent", "skip"), context)
    assert references_available(optional("null", fallback=_ref("step", "unavailable", [])), context)
    context["steps"]["a_source01"]["status"] = "failed"
    with pytest.raises(BindingResolutionError) as caught:
        resolve_value(optional("absent"), context)
    assert caught.value.code == "failed_binding_source"


def test_constants_and_run_variables_have_independent_values_and_atomic_assignment():
    rule = {
        "constants": [
            {"id": "c_folder01", "name": "输出目录", "value_type": "path", "value": "D:/results"},
            {"id": "c_values01", "name": "初始列表", "value_type": {"type": "array", "items": "int"}, "value": [1]},
        ],
        "variables": [
            {"id": "v_values01", "name": "当前列表", "value_type": {"type": "array", "items": "int"}, "initial": _ref("constant", "c_values01", [])},
            {"id": "v_total001", "name": "合计", "value_type": {"type": "int", "nullable": True}},
        ],
        "condition": {"type": "path_exists", "params": {"path": _ref("constant", "c_folder01", [])}},
    }
    first, second = {}, {}
    initialize_variables(rule, first)
    initialize_variables(rule, second)
    assert resolve_trigger_constants(rule)["condition"]["params"]["path"] == "D:/results"
    assert "$ref" in rule["condition"]["params"]["path"]
    value = list(resolve_value(_ref("variable", "v_values01", []), first))
    value.append(2)
    assign_variable("v_values01", value, first)
    assert first["variables"]["v_values01"] == [1, 2]
    assert second["variables"]["v_values01"] == [1]
    assert first["constants"]["c_values01"] == [1]
    with pytest.raises(DataTypeError):
        assign_variable("v_values01", [False], first)
    assert first["variables"]["v_values01"] == [1, 2]
    with pytest.raises(DataTypeError):
        assign_variable("c_values01", [], first)
    with pytest.raises(BindingResolutionError):
        resolve_value(_ref("variable", "v_total001", []), first)
    assign_variable("v_total001", None, first)
    assert resolve_value(_ref("variable", "v_total001", []), first) is None
    initialize_variables(rule, second, overrides={"v_total001": 7})
    assert second["variables"]["v_total001"] == 7


def test_constant_dependencies_are_checked_before_run_initialization():
    rule = {"constants": [
        {"id": "c_first001", "name": "第一项", "value_type": "text", "value": _ref("constant", "c_second01", [])},
        {"id": "c_second01", "name": "第二项", "value_type": "text", "value": "value", "sensitive": True},
    ]}
    context = {}
    initialize_variables(rule, context)
    assert context["constants"]["c_first001"] == "value"
    rule["constants"][1]["value"] = _ref("constant", "c_first001", [])
    with pytest.raises(DataTypeError) as caught:
        initialize_variables(rule, {})
    assert caught.value.code == "cyclic_constant"
    rule["constants"][1]["value"] = _ref("variable", "v_future01", [])
    with pytest.raises(DataTypeError):
        initialize_variables(rule, {})


@pytest.mark.parametrize("kind,plugin_id,parameter", [
    ("triggers", "battery_level", "threshold"),
    ("triggers", "system_resource", "threshold"),
    ("actions", "text_to_speech", "rate"),
    ("actions", "text_to_speech", "volume"),
])
@pytest.mark.parametrize("value", [True, False, "0.5", None, [], {}, 0, 0.5])
def test_builtin_numeric_parameters_validate_resolved_constants(kind, plugin_id, parameter, value):
    from notmyfault.core.runtime_rules import prepare_rules
    from notmyfault.core.workflow import invoke_action

    folder = Path(__file__).resolve().parents[1] / kind / plugin_id
    filename = "trigger.json" if kind == "triggers" else "action.json"
    meta = json.loads((folder / filename).read_text(encoding="utf-8"))
    params = {parameter: _ref("constant", "c_value001", [])}
    if plugin_id == "system_resource":
        params["disk_path"] = "."
    if kind == "actions":
        params["text"] = "播报内容"
    rule = {
        "name": "数字参数",
        "rule_id": "r_numeric1",
        "constants": [{"id": "c_value001", "name": "参数值", "value_type": "any", "value": value}],
        "condition": {"type": plugin_id if kind == "triggers" else "signal", "params": params if kind == "triggers" else {}},
        "actions": [{"type": plugin_id if kind == "actions" else "noop", "params": params if kind == "actions" else {}}],
    }
    valid = not isinstance(value, bool) and isinstance(value, (int, float))
    prepared = prepare_rules(
        [rule], {plugin_id: meta} if kind == "triggers" else {"signal": {"params": []}},
        {plugin_id: meta} if kind == "actions" else {"noop": {"params": []}},
    )
    if kind == "triggers":
        assert len(prepared) == int(valid), prepared.issues
        if valid:
            actual = prepared.trigger_params[plugin_id][0][parameter]
            assert actual == value and type(actual) is type(value)
        else:
            assert any(parameter in issue for _name, issue in prepared.issues)
        return
    assert len(prepared) == 1, prepared.issues
    context = build_context(rule["name"], "signal", {}, [])
    prepared.prepare_context(prepared.rules[0], context)
    resolved = resolve_value(params, context)
    calls = []
    if valid:
        invoke_action(lambda info, actual: calls.append(actual), None, meta, resolved, context)
        assert len(calls) == 1
        assert calls[0][parameter] == value and type(calls[0][parameter]) is type(value)
    else:
        with pytest.raises(DataTypeError, match=parameter):
            invoke_action(lambda info, actual: calls.append(actual), None, meta, resolved, context)
        assert calls == []


def test_typed_nested_bindings_conversion_and_optional_policy_are_checked_on_save():
    actions_meta = {
        "source": {"outputs": [{"name": "rows", "type": "array", "value_type": {"type": "array", "items": {"type": "object", "properties": {"count": "int"}, "required": ["count"], "additional_properties": False}}}]},
        "consume": {"params": [{"name": "record", "type": "textarea", "value_type": {"type": "object", "properties": {"text": "text"}, "required": ["text"]}}]},
    }
    reference = {"$ref": {"scope": "step", "node": "a_source01", "path": ["rows", 0, "count"], "on_missing": "error"}}
    rule = _rule(_leaf("usb_insert", "t_usb001"), [
        {"type": "source", "binding_id": "a_source01", "params": {}},
        {"type": "consume", "binding_id": "a_consume1", "params": {"record": {"text": reference}}},
    ])
    issues = validate_rule_bindings(rule, TRIGGERS_META, actions_meta)
    assert [issue["code"] for issue in issues] == ["binding_type_mismatch"]
    assert issues[0]["location"] == "actions[1].params.record.text"
    rule["actions"][1]["params"]["record"]["text"] = {"$convert": {"value": reference, "to": "text"}}
    assert validate_rule_bindings(rule, TRIGGERS_META, actions_meta) == []
    reference["$ref"].pop("on_missing")
    assert [issue["code"] for issue in validate_rule_bindings(rule, TRIGGERS_META, actions_meta)] == ["optional_output"]


def test_event_occurred_checks_workflow_identity_without_requiring_outputs():
    from notmyfault.core.predicates import evaluate_predicate, validate_predicate
    source = {"op": "event_occurred", "trigger_id": "t_hot001"}
    rule = _rule(_leaf("hotkey", "t_hot001"), [{"type": "if", "binding_id": "a_branch01", "condition": source,
                 "then": [{"type": "notify", "binding_id": "a_notify01", "params": {}}], "else": []}])
    assert validate_rule_bindings(rule, {"hotkey": {"outputs": []}}, ACTIONS_META) == []
    matched = build_context("规则", "hotkey", {}, [{"binding_id": "t_hot001", "event": {"type": "hotkey"}, "payload": {}}])
    assert evaluate_predicate(source, matched)
    assert not evaluate_predicate(source, build_context("规则", "other", {}, []))
    assert evaluate_predicate({**source, "expected": False}, build_context("规则", "other", {}, []))
    matched["manual_test"] = {"provided_trigger_ids": ()}
    assert not evaluate_predicate(source, matched)
    matched["manual_test"]["provided_trigger_ids"] = ("t_hot001",)
    assert evaluate_predicate(source, matched)
    source["trigger_id"] = "t_unknown1"
    assert [issue["code"] for issue in validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META)] == ["unknown_source"]
    assert validate_predicate({"op": "event_occurred", "trigger_id": "a_action1"})
    assert validate_predicate({"op": "event_occurred", "trigger_id": "t_hot001", "expected": "false"})
