"""规则匹配、条件树校验与规则验证分发"""

import threading

import pytest

from notmyfault.core.engine import AutomationEngine
from notmyfault.core.rules import (
    ConditionRuntime,
    get_rule_events,
    validate_rule_binding_ids,
    validate_rule_structure,
    validate_rules_structure,
)


def make_engine(rules=None, on_event=None):
    engine = AutomationEngine({"rules": rules or []}, on_event=on_event)
    engine._alert_user = lambda *a, **k: None
    return engine


def register_action(engine, action_type, func, meta=None):
    engine.actions_funcs[action_type] = func
    engine.actions_meta[action_type] = meta or {}


class TestCallNotmyfault:
    def test_missing_trigger_id(self):
        ran = []
        engine = make_engine(
            rules=[{
                "name": "r",
                "event": {"type": "hotkey", "params": {}},
                "actions": [{"type": "noop", "params": {}}],
            }]
        )
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine.call_notmyfault({"triggered_params": {}})
        engine.call_notmyfault({"trigger_id": 123, "triggered_params": {}})
        assert ran == []

    def test_valid_event_forwarding(self):
        ran = []
        engine = make_engine(
            rules=[{
                "name": "r",
                "event": {"type": "hotkey", "params": {}},
                "actions": [{"type": "noop", "params": {}}],
            }]
        )
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine.call_notmyfault({"trigger_id": "hotkey", "triggered_params": {}})
        assert ran == [1]


class TestConditionRuntime:
    def test_all_condition_respects_time_window(self):
        runtime = ConditionRuntime()
        rule = {"condition": {
            "op": "all",
            "within_seconds": 10,
            "children": [
                {"type": "evt_a", "params": {}},
                {"type": "evt_b", "params": {}},
            ],
        }}
        assert runtime.match("r", rule, "evt_a", {}, now=0.0) is False
        # 第二个事件超出时间窗口，组合不能成立
        assert runtime.match("r", rule, "evt_b", {}, now=50.0) is False
        assert runtime.match("r", rule, "evt_b", {}, now=5.0) is True

    def test_nested_events_are_aggregated(self):
        rule = {"condition": {
            "op": "all",
            "children": [
                {"type": "evt_a", "params": {}},
                {"op": "any", "children": [
                    {"type": "evt_b", "params": {}},
                    {"type": "evt_c", "params": {}},
                ]},
            ],
        }}
        events = get_rule_events(rule)
        assert [e["type"] for e in events] == ["evt_a", "evt_b", "evt_c"]

    def test_old_or_condition_stays_compatible(self):
        # 早期格式用 type: or 和 events 字段，仍按 any 语义匹配
        rule = {"condition": {"type": "or", "events": [
            {"type": "evt_x", "params": {}},
            {"type": "evt_y", "params": {}},
        ]}}
        runtime = ConditionRuntime()
        assert runtime.match("r", rule, "evt_y", {}) is True
        runtime.reset()
        assert runtime.match("r", rule, "evt_x", {}) is True


class TestEventDispatching:
    def test_matching_event_dispatches_action(self):
        ran = []
        engine = make_engine(
            rules=[{
                "name": "r",
                "event": {"type": "hotkey", "params": {"key": "f1"}},
                "actions": [{"type": "noop", "params": {}}],
            }]
        )
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine.emit_event("hotkey", {"key": "f1"})
        assert ran == [1]

    def test_non_matching_type_no_dispatch(self):
        ran = []
        engine = make_engine(
            rules=[{
                "name": "r",
                "event": {"type": "hotkey", "params": {}},
                "actions": [{"type": "noop", "params": {}}],
            }]
        )
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine.emit_event("usb_insert", {})
        assert ran == []

    def test_non_matching_params_no_dispatch(self):
        ran = []
        engine = make_engine(
            rules=[{
                "name": "r",
                "event": {"type": "hotkey", "params": {"key": "f1"}},
                "actions": [{"type": "noop", "params": {}}],
            }]
        )
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine.emit_event("hotkey", {"key": "f2"})
        assert ran == []

    def test_missing_params_no_dispatch(self):
        ran = []
        engine = make_engine(
            rules=[{
                "name": "r",
                "event": {"type": "hotkey", "params": {"key": "f1"}},
                "actions": [{"type": "noop", "params": {}}],
            }]
        )
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine.emit_event("hotkey", {})
        assert ran == []

    def test_extra_params_no_block(self):
        ran = []
        engine = make_engine(
            rules=[{
                "name": "r",
                "event": {"type": "hotkey", "params": {"key": "f1"}},
                "actions": [{"type": "noop", "params": {}}],
            }]
        )
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine.emit_event("hotkey", {"key": "f1", "extra": "x"})
        assert ran == [1]

    def test_partial_params_match(self):
        ran = []
        engine = make_engine(
            rules=[{
                "name": "r",
                "event": {"type": "hotkey", "params": {"key": "f1"}},
                "actions": [{"type": "noop", "params": {}}],
            }]
        )
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine.emit_event("hotkey", {"key": "f1", "mods": ["ctrl"]})
        assert ran == [1]

    def test_multiple_actions_in_single_rule(self):
        order = []
        engine = make_engine(
            rules=[{
                "name": "r",
                "event": {"type": "hotkey", "params": {}},
                "actions": [
                    {"type": "first", "params": {}},
                    {"type": "second", "params": {}},
                ],
            }]
        )
        register_action(engine, "first", lambda meta, params: order.append("first"))
        register_action(engine, "second", lambda meta, params: order.append("second"))
        engine.emit_event("hotkey", {})
        assert order == ["first", "second"]

    def test_multiple_rules_same_event(self):
        ran = []
        engine = make_engine(rules=[
            {
                "name": "r1",
                "event": {"type": "hotkey", "params": {}},
                "actions": [{"type": "noop", "params": {"tag": 1}}],
            },
            {
                "name": "r2",
                "event": {"type": "hotkey", "params": {}},
                "actions": [{"type": "noop", "params": {"tag": 2}}],
            },
        ])
        register_action(engine, "noop", lambda meta, params: ran.append(params["tag"]))
        engine.emit_event("hotkey", {})
        assert ran == [1, 2]

    def test_nested_all_condition_requires_both_events(self):
        ran = []
        engine = make_engine(rules=[{
            "name": "r",
            "condition": {
                "op": "all",
                "children": [
                    {"type": "evt_a", "params": {}},
                    {"type": "evt_b", "params": {}},
                ],
            },
            "actions": [{"type": "noop", "params": {}}],
        }])
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine.emit_event("evt_a", {})
        assert ran == []
        engine.emit_event("evt_b", {})
        assert ran == [1]

    def test_rule_with_old_trigger_key(self):
        ran = []
        engine = make_engine(rules=[{
            "name": "r",
            "trigger": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }])
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine.emit_event("hotkey", {})
        assert ran == [1]

    def test_emit_no_rules(self):
        engine = make_engine()
        engine.emit_event("hotkey", {})

    def test_empty_action_list_no_crash(self):
        engine = make_engine(rules=[{
            "name": "r",
            "event": {"type": "hotkey", "params": {}},
            "actions": [],
        }])
        engine.emit_event("hotkey", {})

    def test_emit_during_shutdown(self):
        ran = []
        engine = make_engine(rules=[{
            "name": "r",
            "event": {"type": "hotkey", "params": {}},
            "actions": [{"type": "noop", "params": {}}],
        }])
        register_action(engine, "noop", lambda meta, params: ran.append(1))
        engine._shutdown_flag = threading.Event()
        engine._shutdown_flag.set()
        engine.emit_event("hotkey", {})
        assert ran == []

    def test_on_event_callback_fires(self):
        events = []
        engine = make_engine(
            rules=[{
                "name": "r",
                "event": {"type": "hotkey", "params": {}},
                "actions": [{"type": "noop", "params": {}}],
            }],
            on_event=lambda t, p: events.append((t, p)),
        )
        register_action(engine, "noop", lambda meta, params: None)
        engine.emit_event("hotkey", {})
        triggered = [p for t, p in events if t == "rule_triggered"]
        assert len(triggered) == 1
        assert triggered[0]["rule_name"] == "r"
        assert triggered[0]["event_type"] == "hotkey"


class TestRuleStructureValidation:
    @pytest.mark.parametrize(
        ("rule", "expected"),
        [
            pytest.param(
                {
                    "name": "r",
                    "condition": {"op": "any", "children": []},
                    "actions": [{"type": "noop"}],
                },
                "至少需要一个子条件",
                id="rule0-至少需要一个子条件",
            ),
            pytest.param(
                {
                    "name": "r",
                    "condition": {
                        "op": "all",
                        "within_seconds": 0,
                        "children": [{"type": "hotkey", "params": {}}],
                    },
                    "actions": [{"type": "noop"}],
                },
                "within_seconds 必须大于 0",
                id="rule1-within_seconds 必须大于 0",
            ),
            pytest.param(
                {
                    "name": "r",
                    "event": {"type": "hotkey", "params": {}},
                    "actions": [],
                },
                "actions 至少需要一个动作",
                id="rule2-actions 至少需要一个动作",
            ),
        ],
    )
    def test_invalid_rule(self, rule, expected):
        errors = validate_rule_structure(rule)
        assert any(expected in error for error in errors)

    def test_valid_nested_rule(self):
        rule = {
            "name": "r",
            "condition": {
                "op": "all",
                "children": [
                    {"type": "evt_a", "params": {"k": 1}},
                    {"op": "any", "children": [
                        {"type": "evt_b", "params": {}},
                        {"type": "evt_c", "params": {}},
                    ]},
                ],
            },
            "actions": [{"type": "noop", "params": {}}],
        }
        assert validate_rule_structure(rule) == []


class TestValidateAllRules:
    def _engine_with_meta(self, rules, action_meta=None):
        engine = make_engine(rules=rules)
        engine.triggers_meta["hotkey"] = {}
        register_action(engine, "noop", lambda meta, params: None, action_meta)
        alerts = []
        engine._alert_user = lambda title, message, *a, **k: alerts.append((title, message))
        return engine, alerts

    def _rule(self, params=None, action_type="noop"):
        return {
            "name": "r",
            "event": {"type": "hotkey", "params": {}},
            "actions": [{"type": action_type, "params": params or {}}],
        }

    def test_empty_rules(self):
        engine = make_engine()
        assert engine._validate_all_rules() == (0, 0)

    def test_all_valid_rules(self):
        engine, alerts = self._engine_with_meta([self._rule()])
        assert engine._validate_all_rules() == (1, 1)
        assert alerts == []

    def test_no_triggers_match(self):
        engine, alerts = self._engine_with_meta([
            {
                "name": "r",
                "event": {"type": "ghost_trigger", "params": {}},
                "actions": [{"type": "noop", "params": {}}],
            }
        ])
        valid, total = engine._validate_all_rules()
        assert (valid, total) == (0, 1)
        assert len(alerts) == 1

    def test_action_referencing_unloaded_plugin(self):
        engine, alerts = self._engine_with_meta([self._rule(action_type="ghost")])
        valid, total = engine._validate_all_rules()
        assert (valid, total) == (0, 1)
        assert engine._diag_obj.data["rule_issues"]

    def test_action_missing_type(self):
        engine, alerts = self._engine_with_meta([
            {
                "name": "r",
                "event": {"type": "hotkey", "params": {}},
                "actions": [{"params": {}}],
            }
        ])
        valid, total = engine._validate_all_rules()
        assert (valid, total) == (0, 1)
        assert len(alerts) == 1

    def test_alert_called_for_rule_issues(self):
        engine, alerts = self._engine_with_meta([self._rule(action_type="ghost")])
        engine._validate_all_rules()
        assert alerts and alerts[0][0] == "规则配置异常"

    def test_param_type_mismatch_number(self, capsys):
        meta = {"params": [{"name": "count", "type": "number"}]}
        engine, alerts = self._engine_with_meta(
            [self._rule(params={"count": "不是数字"})], meta
        )
        valid, total = engine._validate_all_rules()
        # 类型不匹配只是警告，规则仍然有效
        assert (valid, total) == (1, 1)
        assert "应为数字" in capsys.readouterr().err

    def test_param_type_mismatch_bool(self, capsys):
        meta = {"params": [{"name": "enabled", "type": "bool"}]}
        engine, alerts = self._engine_with_meta(
            [self._rule(params={"enabled": "yes"})], meta
        )
        valid, total = engine._validate_all_rules()
        assert (valid, total) == (1, 1)
        assert "应为布尔值" in capsys.readouterr().err

    def test_select_param_valid_value(self, capsys):
        meta = {"params": [{
            "name": "mode",
            "type": "select",
            "options": [{"value": "fast", "label": "快"}, {"value": "slow", "label": "慢"}],
        }]}
        engine, alerts = self._engine_with_meta(
            [self._rule(params={"mode": "fast"})], meta
        )
        assert engine._validate_all_rules() == (1, 1)
        assert "不在可选项中" not in capsys.readouterr().err

    def test_select_param_invalid_value(self, capsys):
        meta = {"params": [{
            "name": "mode",
            "type": "select",
            "options": [{"value": "fast", "label": "快"}, {"value": "slow", "label": "慢"}],
        }]}
        engine, alerts = self._engine_with_meta(
            [self._rule(params={"mode": "ludicrous"})], meta
        )
        valid, total = engine._validate_all_rules()
        assert (valid, total) == (1, 1)
        assert "不在可选项中" in capsys.readouterr().err

    def test_unknown_param_name_warns(self, capsys):
        meta = {"params": [{"name": "count", "type": "number"}]}
        engine, alerts = self._engine_with_meta(
            [self._rule(params={"no_such": 1})], meta
        )
        valid, total = engine._validate_all_rules()
        assert (valid, total) == (1, 1)
        assert "使用了未知参数" in capsys.readouterr().err


def test_validate_rules_ignores_legacy_step_ids():
    # 无 binding_id 的旧格式规则不做节点身份校验
    rules = [{
        "name": "r",
        "event": {"type": "hotkey", "params": {}},
        "actions": [{"type": "noop", "params": {}}],
    }]
    assert validate_rules_structure(rules) == []


def test_validate_rules_rejects_duplicate_or_invalid_step_ids():
    duplicated = {
        "name": "r",
        "event": {"type": "hotkey", "params": {}, "binding_id": "t_hot001"},
        "actions": [
            {"type": "noop", "binding_id": "a_dup001", "params": {}},
            {"type": "noop", "binding_id": "a_dup001", "params": {}},
        ],
    }
    errors = validate_rule_binding_ids(duplicated)
    assert any("重复" in error for error in errors)

    invalid = {
        "name": "r",
        "event": {"type": "hotkey", "params": {}, "binding_id": "t_hot001"},
        "actions": [{"type": "noop", "binding_id": "BAD ID", "params": {}}],
    }
    errors = validate_rule_binding_ids(invalid)
    assert any("binding_id 无效" in error for error in errors)
    assert validate_rules_structure([invalid])
