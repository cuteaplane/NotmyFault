import pytest

from notmyfault.host.ai_rule_draft import parse_rule_draft
from notmyfault.host.ai_tools import ToolCallError


def make_catalog():
    return {
        "triggers": {
            "time_schedule": {
                "params": [
                    {"name": "time"},
                    {"name": "repeat"},
                ]
            },
            "usb_insert": {"params": []},
        },
        "actions": {
            "notify": {"params": [{"name": "title"}, {"name": "message"}]},
            "open_url": {"params": [{"name": "url"}]},
        },
    }


def rule_args(**overrides):
    args = {
        "name": "晚间提醒",
        "event": {"type": "time_schedule", "params": {"time": "20:00"}},
        "actions": [{"type": "notify", "params": {"message": "检查日报"}}],
    }
    args.update(overrides)
    return args


def test_valid_rule_returns_normalized_draft():
    assert parse_rule_draft(rule_args(), make_catalog()) == rule_args()


@pytest.mark.parametrize(
    "bad_args",
    [
        rule_args(preconditions=[{"type": "open_url", "params": {"url": "https://example.com"}}]),
        {"name": "x", "event": {"type": "time_schedule", "params": {}}, "actions": [], "extra": 1},
        {"event": {"type": "time_schedule", "params": {}}, "actions": []},
        {"name": "", "event": {"type": "time_schedule", "params": {}}, "actions": []},
        {"name": "x", "actions": []},
        {"name": "x", "event": "not-an-object", "actions": []},
        {"name": "x", "event": {"type": "time_schedule", "params": {}}, "actions": "nope"},
        {"name": "x", "event": {"type": "time_schedule", "params": {}}, "actions": [123]},
    ],
)
def test_rejects_malformed_rule_shape(bad_args):
    with pytest.raises(ToolCallError) as caught:
        parse_rule_draft(bad_args, make_catalog())
    assert caught.value.code == "rule_invalid"


@pytest.mark.parametrize(
    "args",
    [
        rule_args(event={"type": "ghost_trigger", "params": {}}),
        rule_args(actions=[{"type": "ghost_action", "params": {}}]),
        rule_args(event={"type": "usb_insert", "params": {"time": "09:00"}}),
        rule_args(actions=[{"type": "notify", "params": {"url": "https://x"}}]),
        rule_args(event={"type": "time_schedule", "params": {}, "code": "x"}),
    ],
)
def test_rejects_unknown_plugins_params_and_fields(args):
    with pytest.raises(ToolCallError) as caught:
        parse_rule_draft(args, make_catalog())
    assert caught.value.code == "rule_invalid"
