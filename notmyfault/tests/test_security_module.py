"""安全模式选择与插件风险扫描。"""

import json

import pytest

from notmyfault.security import security as security_mod
from notmyfault.security.plugin_schema import scan_plugin_security
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
        ("normal", "dev", SecurityMode.NORMAL),
        ("permissive", "develop", SecurityMode.NORMAL),
        ("permissive", "stable", SecurityMode.STRICT),
        ("normal", "master", SecurityMode.STRICT),
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
        ("import ctypes\nctypes.windll.user32.GetForegroundWindow()\n", "native_call"),
        ("import importlib\nimportlib.import_module(name)\n", "dynamic_import"),
        ("import winreg\nwinreg.OpenKey(root, name)\n", "registry_access"),
    ],
)
def test_plugin_risk_scan_detects_public_risk_categories(tmp_path, source, risk_id):
    (tmp_path / "action.py").write_text(source, encoding="utf-8")
    assert risk_id in {risk["id"] for risk in scan_plugin_security(str(tmp_path))}


def test_plugin_risk_scan_ignores_comments_and_strings(tmp_path):
    (tmp_path / "action.py").write_text(
        "# subprocess.run(['tool'])\nmessage = 'eval(data)'\n", encoding="utf-8"
    )
    assert scan_plugin_security(str(tmp_path)) == []
