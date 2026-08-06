"""结构化绑定的静态校验、上下文构建与 guaranteed 来源推导"""

import pytest

from notmyfault.core.bindings import (
    BindingResolutionError,
    iter_references,
    resolve_value,
)
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
}


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


def test_preconditions_reject_conditional_or_source():
    rule = _rule(
        {
            "op": "any",
            "children": [_leaf("usb_insert", "t_usb001"), _leaf("hotkey", "t_hot001")],
        },
        [{"type": "notify", "binding_id": "a_not001", "params": {"message": "x"}}],
        preconditions=[
            {
                "type": "notify",
                "binding_id": "p_che001",
                "params": {"message": _ref("trigger", "t_usb001", ["drive"])},
            }
        ],
    )
    issues = validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META)
    assert [issue["code"] for issue in issues] == ["conditional_source"]


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


def test_static_validation_rejects_conditional_or_source():
    # 开始前确认既不能引用 or 分支触发器，也不能引用步骤结果
    rule = _rule(
        {
            "op": "any",
            "children": [_leaf("usb_insert", "t_usb001"), _leaf("hotkey", "t_hot001")],
        },
        [
            {
                "type": "open_url",
                "binding_id": "a_url001",
                "params": {"url": _ref("step", "a_not001", ["status"])},
            },
            {
                "type": "notify",
                "binding_id": "a_not001",
                "params": {"message": _ref("trigger", "t_hot001", ["keys"])},
            }
        ],
        preconditions=[
            {
                "type": "notify",
                "binding_id": "p_che001",
                "params": {"message": _ref("step", "a_not001", ["status"])},
            }
        ],
    )
    codes = [issue["code"] for issue in validate_rule_bindings(rule, TRIGGERS_META, ACTIONS_META)]
    assert "step_not_available" in codes
    # 动作引用后面的步骤算前向引用
    assert "forward_reference" in codes


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
    # 缺失数据是配置错误，抛 ValueError 子类而不是可重试的运行时异常
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
    # 整串旧模板也保留原类型，不转成字符串
    resolved = resolve_value("{{ triggers.t_usb001.payload.count }}", context)
    assert resolved == 5 and isinstance(resolved, int)
