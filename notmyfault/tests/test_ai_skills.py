import json

from notmyfault.host.ai_skills import build_rule_drafting_skill


def make_catalog():
    return {
        "triggers": {
            "time_schedule": {"name": "定时", "params": []},
            "usb_insert": {"name": "U 盘插入", "params": []},
        },
        "actions": {
            "notify": {"name": "显示通知", "params": []},
            "open_url": {"name": "打开网址", "params": []},
        },
    }


def tool_schema(catalog):
    skill = build_rule_drafting_skill(catalog)
    return json.loads(json.dumps(skill.tools[0].parameters))


def test_rule_schema_tracks_catalog():
    schema = tool_schema(make_catalog())
    assert schema["properties"]["event"]["properties"]["type"]["enum"] == [
        "time_schedule",
        "usb_insert",
    ]
    assert schema["properties"]["actions"]["items"]["properties"]["type"]["enum"] == [
        "notify",
        "open_url",
    ]


def test_rule_schema_rejects_extra_fields():
    schema = tool_schema(make_catalog())
    assert schema["additionalProperties"] is False
    assert schema["properties"]["event"]["additionalProperties"] is False
    assert schema["properties"]["actions"]["items"]["additionalProperties"] is False
