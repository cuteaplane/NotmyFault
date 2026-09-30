"""environment_variable 动作的读写与变量名校验"""

import importlib.util
import json
from pathlib import Path

import pytest

from notmyfault.security.plugin_schema import validate_plugin_meta


PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PKG_ROOT / "actions" / "environment_variable"


def load_module():
    path = PLUGIN_ROOT / "action.py"
    spec = importlib.util.spec_from_file_location("environment_variable_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_is_cross_platform_without_capabilities():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    valid, errors = validate_plugin_meta(meta, "action")
    assert valid, errors
    assert meta["id"] == "environment_variable"
    assert meta["name"] == "环境变量"
    assert meta["platforms"] == ["windows", "linux"]
    assert meta["permissions"] == []
    assert "requires_capabilities" not in meta
    assert meta["idempotent"] is False
    assert meta["enabled"] is True
    assert meta["package_name"] == "io.github.notmyfault.environment_variable"
    assert meta["version"] == "1.0"
    assert meta["version_code"] == 1


def test_manifest_requires_admin_approval_for_setting_variables():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    assert meta["security"]["rule_approval"] == "admin_key"

    from notmyfault.security import rule_approval

    rule = {
        "rule_id": "r_env0001",
        "name": "改 PATH",
        "event": {"type": "plain_trigger", "params": {}},
        "actions": [
            {
                "type": "environment_variable",
                "params": {"mode": "set", "name": "PATH", "value": "/tmp"},
            }
        ],
    }
    schema = {
        "triggers": {"plain_trigger": {"permissions": []}},
        "actions": {"environment_variable": meta},
    }
    assert rule_approval.changed_admin_plugins([], [rule], schema) == [
        "environment_variable"
    ]


def test_get_existing(monkeypatch):
    module = load_module()
    monkeypatch.setenv("NMF_EV_TEST_EXISTING", "hello")
    result = module.run({}, {"mode": "get", "name": "NMF_EV_TEST_EXISTING"})
    assert result == {
        "name": "NMF_EV_TEST_EXISTING",
        "value": "hello",
        "existed": True,
    }


def test_get_missing_empty(monkeypatch):
    module = load_module()
    monkeypatch.delenv("NMF_EV_TEST_MISSING", raising=False)
    result = module.run(
        {},
        {"mode": "get", "name": "NMF_EV_TEST_MISSING", "missing": "empty"},
    )
    assert result == {
        "name": "NMF_EV_TEST_MISSING",
        "value": "",
        "existed": False,
    }


def test_get_missing_error(monkeypatch):
    module = load_module()
    monkeypatch.delenv("NMF_EV_TEST_MISSING", raising=False)
    with pytest.raises(ValueError, match="环境变量不存在"):
        module.run(
            {},
            {"mode": "get", "name": "NMF_EV_TEST_MISSING", "missing": "error"},
        )


def test_set_creates_and_overwrites(monkeypatch):
    module = load_module()
    monkeypatch.delenv("NMF_EV_TEST_SET", raising=False)
    created = module.run(
        {},
        {"mode": "set", "name": "NMF_EV_TEST_SET", "value": "one"},
    )
    assert created == {
        "name": "NMF_EV_TEST_SET",
        "value": "one",
        "existed": False,
    }
    overwritten = module.run(
        {},
        {"mode": "set", "name": "NMF_EV_TEST_SET", "value": "two"},
    )
    assert overwritten == {
        "name": "NMF_EV_TEST_SET",
        "value": "two",
        "existed": True,
    }
    assert module.run({}, {"mode": "get", "name": "NMF_EV_TEST_SET"})["value"] == "two"


def test_set_allows_empty_value(monkeypatch):
    module = load_module()
    monkeypatch.delenv("NMF_EV_TEST_EMPTY", raising=False)
    result = module.run(
        {},
        {"mode": "set", "name": "NMF_EV_TEST_EMPTY", "value": ""},
    )
    assert result == {
        "name": "NMF_EV_TEST_EMPTY",
        "value": "",
        "existed": False,
    }
    assert module.run({}, {"mode": "get", "name": "NMF_EV_TEST_EMPTY"}) == {
        "name": "NMF_EV_TEST_EMPTY",
        "value": "",
        "existed": True,
    }


def test_unset_present_and_absent(monkeypatch):
    module = load_module()
    monkeypatch.setenv("NMF_EV_TEST_UNSET", "gone")
    present = module.run({}, {"mode": "unset", "name": "NMF_EV_TEST_UNSET"})
    assert present == {"name": "NMF_EV_TEST_UNSET", "existed": True}
    absent = module.run({}, {"mode": "unset", "name": "NMF_EV_TEST_UNSET"})
    assert absent == {"name": "NMF_EV_TEST_UNSET", "existed": False}


def test_reject_bad_names():
    module = load_module()
    cases = [
        ("", "不能为空"),
        (" leading", "首尾不能有空格"),
        ("trailing ", "首尾不能有空格"),
        ("HAS=EQ", "等号"),
        ("HAS\0NUL", "空字符"),
    ]
    for name, match in cases:
        with pytest.raises(ValueError, match=match):
            module.run({}, {"mode": "get", "name": name})


def test_reject_non_string_name_and_unknown_mode():
    module = load_module()
    with pytest.raises(ValueError, match="变量名必须是文本"):
        module.run({}, {"mode": "get", "name": 123})
    with pytest.raises(ValueError, match="未知操作"):
        module.run({}, {"mode": "append", "name": "NMF_EV_TEST"})
