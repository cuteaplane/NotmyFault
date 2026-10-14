"""安全模式选择与插件风险扫描。"""

import json

import pytest

from notmyfault.security import security as security_mod
from notmyfault.security.plugins import analyze_plugin_source
from notmyfault.security.security import SecurityMode


def set_signed_mode(tmp_path, monkeypatch, mode):
    build_path = tmp_path / "build.json"
    build_path.write_text(json.dumps({"security_mode": mode}), encoding="utf-8")
    monkeypatch.setattr(security_mod, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "notmyfault.security.signing.verify_file",
        lambda path: path == str(build_path),
    )


@pytest.mark.parametrize(
    ("signed_mode", "environment", "expected"),
    [
        ("strict", "alpha", SecurityMode.STRICT),
        ("permissive", "dev", SecurityMode.PERMISSIVE),
        ("permissive", "develop", SecurityMode.PERMISSIVE),
        ("permissive", "stable", SecurityMode.STRICT),
        ("unknown", "master", SecurityMode.STRICT),
        ("permissive", "  ALPHA ", SecurityMode.PERMISSIVE),
    ],
)
def test_environment_can_raise_but_not_lower_signed_mode(
    tmp_path, monkeypatch, signed_mode, environment, expected
):
    set_signed_mode(tmp_path, monkeypatch, signed_mode)
    monkeypatch.setenv("NOTMYFAULT_MODE", environment)
    assert security_mod.detect_security_mode() is expected


def test_missing_signed_build_defaults_to_strict(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTMYFAULT_MODE", raising=False)
    monkeypatch.setattr(security_mod, "_PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    assert security_mod.detect_security_mode() is SecurityMode.STRICT


@pytest.mark.parametrize(
    ("source", "risk_id"),
    [
        ("eval(user_input)\n", "code_injection"),
        ("import subprocess as p\np.run(['dir'])\n", "subprocess"),
        ("open('out.txt', 'w')\n", "file_write"),
        ("import requests\nrequests.get(url)\n", "network_request"),
        ("import urllib.request as request\nrequest.urlopen(url)\n", "network_request"),
        ("from subprocess import run as launch\nlaunch(['dir'])\n", "subprocess"),
        ("import ctypes\nctypes.windll.user32.GetForegroundWindow()\n", "native_call"),
        ("import importlib\nimportlib.import_module(name)\n", "dynamic_import"),
        ("import winreg\nwinreg.OpenKey(root, name)\n", "registry_access"),
    ],
)
def test_plugin_risk_scan_detects_public_risk_categories(source, risk_id):
    risks = analyze_plugin_source(source, "action.py").risks
    assert risk_id in {risk["id"] for risk in risks}
    assert all(risk["file"] == "action.py" for risk in risks)


def test_plugin_risk_scan_ignores_comments_and_strings():
    analysis = analyze_plugin_source("# subprocess.run(['tool'])\nmessage = 'eval(data)'\n")
    assert analysis.risks == []


def test_permissive_unsigned_plugin_warns_but_forbidden_code_still_fails(tmp_path):
    from notmyfault.security.plugin_checks import PluginInspection, PluginTree, evaluate_plugin, inspection_report

    entrypoint = str(tmp_path / "action.py")
    tree = PluginTree([], {}, {entrypoint: b""}, b"", {})
    result = PluginInspection(tmp_path, "action", "user", tree, platform_compatible=True, entrypoint=entrypoint)
    allowed = evaluate_plugin(result, SecurityMode.PERMISSIVE)
    assert allowed["allowed"]
    assert "签名无效，降级加载" in allowed["warnings"]
    assert not evaluate_plugin(result, SecurityMode.STRICT)["allowed"]
    assert "signature" not in inspection_report(result, SecurityMode.PERMISSIVE)
    result.capabilities.add("dynamic_exec")
    blocked = evaluate_plugin(result, SecurityMode.PERMISSIVE)
    assert not blocked["allowed"]
    assert any("dynamic_exec" in error for error in blocked["errors"])
