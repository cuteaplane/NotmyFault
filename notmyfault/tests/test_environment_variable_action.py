"""environment_variable 动作的读写与变量名校验"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from notmyfault.tests.canvas_fixtures import native_rule
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
    assert meta["permissions"] == ["native_api"]
    assert "requires_capabilities" not in meta
    assert meta["idempotent"] is False
    assert meta["package_name"] == "io.github.notmyfault.environment_variable"
    assert meta["version"] == "1.0"
    assert meta["version_code"] == 1


def test_manifest_requires_admin_approval_for_setting_variables():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    assert meta["security"]["rule_approval"] == "admin_key"

    from notmyfault.security import rule_approval

    rule = native_rule({"type": "plain_trigger", "params": {}}, [
            {
                "type": "environment_variable",
                "params": {"mode": "set", "name": "PATH", "value": "/tmp"},
            }
        ], rule_id="r_env0001", name="改 PATH")
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
    result = module.run_with_context({}, {"mode": "get", "name": "NMF_EV_TEST_EXISTING"}, {})
    assert result == {
        "name": "NMF_EV_TEST_EXISTING",
        "value": "hello",
        "existed": True,
    }


def test_get_missing_empty(monkeypatch):
    module = load_module()
    monkeypatch.delenv("NMF_EV_TEST_MISSING", raising=False)
    result = module.run_with_context(
        {},
        {"mode": "get", "name": "NMF_EV_TEST_MISSING", "missing": "empty"},
        {},
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
        module.run_with_context(
            {},
            {"mode": "get", "name": "NMF_EV_TEST_MISSING", "missing": "error"},
            {},
        )


def test_set_creates_and_overwrites(monkeypatch):
    module = load_module()
    monkeypatch.delenv("NMF_EV_TEST_SET", raising=False)
    created = module.run_with_context(
        {},
        {"mode": "set", "name": "NMF_EV_TEST_SET", "value": "one"},
        {},
    )
    assert created == {
        "name": "NMF_EV_TEST_SET",
        "value": "one",
        "existed": False,
    }
    overwritten = module.run_with_context(
        {},
        {"mode": "set", "name": "NMF_EV_TEST_SET", "value": "two"},
        {},
    )
    assert overwritten == {
        "name": "NMF_EV_TEST_SET",
        "value": "two",
        "existed": True,
    }
    assert module.run_with_context({}, {"mode": "get", "name": "NMF_EV_TEST_SET"}, {})["value"] == "two"


def test_set_allows_empty_value(monkeypatch):
    module = load_module()
    monkeypatch.delenv("NMF_EV_TEST_EMPTY", raising=False)
    result = module.run_with_context(
        {},
        {"mode": "set", "name": "NMF_EV_TEST_EMPTY", "value": ""},
        {},
    )
    assert result == {
        "name": "NMF_EV_TEST_EMPTY",
        "value": "",
        "existed": False,
    }
    assert module.run_with_context({}, {"mode": "get", "name": "NMF_EV_TEST_EMPTY"}, {}) == {
        "name": "NMF_EV_TEST_EMPTY",
        "value": "",
        "existed": True,
    }
    child = "import json, os; print(json.dumps(os.environ.get('NMF_EV_TEST_EMPTY')))"
    try:
        assert json.loads(subprocess.check_output([sys.executable, "-c", child], text=True)) == ""
    finally:
        module.run_with_context({}, {"mode": "unset", "name": "NMF_EV_TEST_EMPTY"}, {})
    assert json.loads(subprocess.check_output([sys.executable, "-c", child], text=True)) is None


def test_unset_present_and_absent(monkeypatch):
    module = load_module()
    monkeypatch.setenv("NMF_EV_TEST_UNSET", "gone")
    present = module.run_with_context({}, {"mode": "unset", "name": "NMF_EV_TEST_UNSET"}, {})
    assert present == {"name": "NMF_EV_TEST_UNSET", "existed": True}
    absent = module.run_with_context({}, {"mode": "unset", "name": "NMF_EV_TEST_UNSET"}, {})
    assert absent == {"name": "NMF_EV_TEST_UNSET", "existed": False}


@pytest.mark.parametrize("mode,method", [("get", "__getitem__"), ("unset", "__delitem__")])
def test_read_and_unset_tolerate_concurrent_removal(monkeypatch, mode, method):
    module = load_module()
    name = "NMF_EV_TEST_CONCURRENT"
    monkeypatch.setenv(name, "original")
    ready = threading.Event()
    resume = threading.Event()
    results = []
    errors = []

    def first_operation():
        try:
            results.append(module.run_with_context({}, {"mode": mode, "name": name}, {}))
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=first_operation, daemon=True)
    original = getattr(type(os.environ), method)

    def overlap(environment, key):
        if method == "__getitem__":
            value = original(environment, key)
        if key == name and threading.current_thread() is worker:
            ready.set()
            if not resume.wait(2):
                raise RuntimeError("并发操作未继续")
        return original(environment, key) if method == "__delitem__" else value

    monkeypatch.setattr(type(os.environ), method, overlap)
    worker.start()
    try:
        assert ready.wait(2)
        del os.environ[name]
    finally:
        resume.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    expected = {"name": name, "existed": False} if mode == "unset" else {"name": name, "value": "original", "existed": True}
    assert results == [expected]
    assert name not in os.environ


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
            module.run_with_context({}, {"mode": "get", "name": name}, {})


def test_reject_non_string_name_and_unknown_mode():
    module = load_module()
    with pytest.raises(ValueError, match="变量名必须是文本"):
        module.run_with_context({}, {"mode": "get", "name": 123}, {})
    with pytest.raises(ValueError, match="未知操作"):
        module.run_with_context({}, {"mode": "append", "name": "NMF_EV_TEST"}, {})
