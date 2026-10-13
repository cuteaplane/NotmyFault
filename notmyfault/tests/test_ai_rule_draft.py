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
    trigger = {'type': 'trigger', 'binding_id': 'a_schedule01', 'trigger': {'binding_id': 't_schedule01', 'type': 'time_schedule', 'params': {'time': '20:00'}}}
    action = {'type': 'notify', 'binding_id': 'a_notice001', 'params': {'message': '检查日报'}}
    args = {'name': '晚间提醒', 'workflow': {'entry': None, 'nodes': [trigger, action], 'edges': [{'source': 'a_schedule01', 'port': 'event', 'target': 'a_notice001', 'channel': 'control'}]}}
    args.update(overrides)
    return args


def test_valid_rule_returns_normalized_draft():
    original = rule_args()
    assert parse_rule_draft(original, make_catalog()) == original
    assert parse_rule_draft(original, make_catalog()) == original


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


@pytest.mark.parametrize('target, field, value', [
    ('trigger', 'type', 'ghost_trigger'),
    ('action', 'type', 'ghost_action'),
    ('trigger', 'params', {'unknown': '09:00'}),
    ('action', 'params', {'url': 'https://x'}),
    ('trigger', 'code', 'x'),
])
def test_rejects_unknown_plugins_params_and_fields(target, field, value):
    args = rule_args()
    node = args['workflow']['nodes'][0]['trigger'] if target == 'trigger' else args['workflow']['nodes'][1]
    node[field] = value
    with pytest.raises(ToolCallError) as caught:
        parse_rule_draft(args, make_catalog())
    assert caught.value.code == 'rule_invalid'
