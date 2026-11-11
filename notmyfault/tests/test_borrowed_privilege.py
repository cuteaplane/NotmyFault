"""借壳提权扫描：跨插件模块导入、sys.modules 与内置动态执行绕过"""

import json
from types import SimpleNamespace

import pytest

from notmyfault.security.plugins import analyze_plugin_source
from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
from notmyfault.security.security import SecurityMode


def scan(source):
    return analyze_plugin_source(source, "action.py").borrowed


def test_plain_plugin_no_findings():
    assert scan("def run_with_context(action_info, params, context):\n    return True\n") == []


def test_import_notmyfault_public_api_no_findings():
    source = (
        "import notmyfault\n"
        "import notmyfault.config\n"
        "from notmyfault.core.logging import engine_info\n"
        "from notmyfault.triggers.base import PollingTrigger\n"
    )
    assert scan(source) == []


def test_import_sudo_via_public_api_no_findings():
    source = "from notmyfault.security.sudo import run_as_admin\n"
    assert scan(source) == []


@pytest.mark.parametrize("module", ["notmyfault.action_other", "notmyfault.actions.other", "notmyfault.triggers.other"])
def test_direct_import_of_engine_plugin_module(module):
    findings = scan(f"import {module}\n")
    assert any(
        f"直接导入引擎插件模块 {module}" in f for f in findings
    )


def test_from_import_of_engine_plugin_module():
    findings = scan("from notmyfault.trigger_hotkey import helper\n")
    assert any(
        "from-import 引擎插件模块 notmyfault.trigger_hotkey 的 helper" in f
        for f in findings
    )


@pytest.mark.parametrize("lookup", ["sys.modules['notmyfault.action_victim']", "sys.modules.get('notmyfault.action_victim')"])
def test_sys_modules_access(lookup):
    findings = scan(
        f"import sys\nvictim = {lookup}\n"
    )
    assert any(
        "通过 sys.modules 获取引擎插件模块 notmyfault.action_victim" in f
        for f in findings
    )


def test_sys_modules_non_plugin_key_no_findings():
    assert scan("import sys\nm = sys.modules['os']\n") == []


def test_sys_modules_getattr_bypass_detected():
    source = (
        "import sys\n"
        "run = getattr(sys.modules['notmyfault.action_victim'], 'run')\n"
    )
    findings = scan(source)
    assert any("sys.modules" in f for f in findings)


def test_sys_modules_subprocess_popen_bypass_detected():
    source = (
        "import sys\n"
        "m = sys.modules['notmyfault.trigger_x']\n"
        "m.Popen('cmd')\n"
    )
    findings = scan(source)
    assert any("sys.modules" in f for f in findings)
    assert any(
        "调用引擎插件模块内部函数 notmyfault.trigger_x.Popen()" in f
        for f in findings
    )


def test_alias_call_of_plugin_module_internal():
    source = (
        "import notmyfault.action_other as other\n"
        "other.helper()\n"
    )
    findings = scan(source)
    assert any(
        "调用引擎插件模块内部函数 notmyfault.action_other.helper()" in f
        for f in findings
    )


def test_getattr_builtins_exec_bypass():
    findings = scan('f = getattr(__builtins__, "exec")\n')
    assert any("getattr 获取内置动态执行函数 exec" in f for f in findings)


def test_getattr_unrelated_no_findings():
    assert scan('name = getattr(obj, "name")\n') == []


@pytest.mark.parametrize("lookup", ['__builtins__["exec"]', 'builtins.__dict__["exec"]', '__builtins__.__dict__["eval"]'])
def test_builtins_subscript_exec_bypass_detected(lookup):
    analysis = analyze_plugin_source(f'import builtins\nf = {lookup}\nf("print(1)")\n', "action.py")
    assert "dynamic_exec" in analysis.capabilities
    assert "code_injection" in {risk["id"] for risk in analysis.risks}


@pytest.mark.parametrize("source", ["from runpy import run_path", "import runpy\nrunpy.run_path('file.py')", "import runpy as rp\nrp.run_module('example')"])
def test_runpy_is_reported_as_dynamic_execution(source):
    analysis = analyze_plugin_source(source, "action.py")
    assert "dynamic_exec" in analysis.capabilities
    assert "code_injection" in {risk["id"] for risk in analysis.risks}

    imported = analyze_plugin_source("import runpy", "action.py")
    assert "dynamic_exec" not in imported.capabilities
    assert "code_injection" not in {risk["id"] for risk in imported.risks}


@pytest.mark.parametrize("method", ["renamed", "borrowed_globals"])
def test_runtime_rejects_changed_name_and_borrowed_globals(tmp_path, monkeypatch, method):
    import types
    from notmyfault.security import sudo
    from notmyfault.security.plugin_imports import PluginImports

    source = b"from notmyfault.security.sudo import run_as_admin\ndef execute():\n    return run_as_admin(['tool.exe'])\n"
    victim_path = tmp_path / "victim.py"
    victim_path.write_bytes(source)
    attacker_path = tmp_path / "attacker.py"
    attacker_source = b"def execute():\n    return helper()\n" if method == "renamed" else source
    attacker_path.write_bytes(attacker_source)
    victim_imports = PluginImports(str(tmp_path), "notmyfault.action_victim", {str(victim_path): source})
    attacker_imports = PluginImports(str(tmp_path), "notmyfault.action_attacker", {str(attacker_path): attacker_source})
    token = sudo.begin_engine_session()
    try:
        victim = victim_imports.load_entry(str(victim_path))
        attacker = attacker_imports.load_entry(str(attacker_path))
        sudo.authorize_plugin("victim", token, module=victim, allowed_executables={"tool.exe"})
        called = []
        monkeypatch.setattr(sudo, "_run_with_uac", lambda *args, **kwargs: called.append(args))
        if method == "renamed":
            attacker.__name__ = "main"
            attacker.helper = victim.execute
            operation = attacker.execute
        else:
            operation = types.FunctionType(attacker.execute.__code__, victim.__dict__)
        with pytest.raises(PermissionError, match="attacker"):
            operation()
        assert called == []
    finally:
        sudo.end_engine_session(token)
        victim_imports.close()
        attacker_imports.close()


def test_builtins_subscript_benign_key_no_false_positive():
    source = 'f = __builtins__["len"]\nf([1])\n'
    assert "dynamic_exec" not in analyze_plugin_source(source, "action.py").capabilities
    assert scan(source) == []


class SudoStub:
    def authorize_plugin(self, plugin_id, token, module=None):
        pass

    def deauthorize_plugin(self, plugin_id, token):
        pass


@pytest.mark.parametrize("origin", ["builtin", "user", "third_party"])
def test_loader_warns_on_borrowed_privilege_for_every_origin(
    tmp_path, monkeypatch, capsys, origin
):
    folder = tmp_path / "actions" / "borrow"
    folder.mkdir(parents=True)
    (folder / "action.json").write_text(
        json.dumps(
            {
                "id": "borrow",
                "name": "借壳插件",
                "description": "测试借壳扫描",
                "execution_api": "context-v1",
                "version_code": 1,
                "version": "1.0",
                "package_name": "com.test.borrow",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (folder / "action.py").write_text(
        "import sys\n"
        "try:\n"
        "    victim = sys.modules['notmyfault.action_victim']\n"
        "except KeyError:\n"
        "    victim = None\n"
        "\n"
        "def run_with_context(action_info, params, context):\n"
        "    return True\n",
        encoding="utf-8",
    )

    integrity_errors = []
    loader = PluginLoader(
        registry=PluginRegistry(),
        config={},
        diagnostics=SimpleNamespace(record_plugin_error=lambda *a: None),
        security_mode=SecurityMode.PERMISSIVE,
        sudo=SudoStub(),
        engine_token="token",
        integrity_errors=integrity_errors,
        plugin_manifest_path=str(tmp_path / "manifest.json"),
    )
    loaded, failed = loader.load(str(tmp_path / "actions"), "action", origin=origin)

    # 所有来源执行相同的借用权限检查；宽松模式记录告警但不拒载。
    assert loaded == 1
    assert failed == 0
    err = capsys.readouterr().err
    assert "借壳提权嫌疑" in err
    assert any("borrow" in w for w in integrity_errors)
