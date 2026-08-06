"""借壳提权扫描：跨插件模块导入、sys.modules 与内置动态执行绕过"""

import json
from pathlib import Path
from types import SimpleNamespace

from notmyfault.security import plugins as security_plugins
from notmyfault.security.plugins import (
    scan_borrowed_privilege,
    scan_plugin_capabilities,
)
from notmyfault.security.plugin_loader import PluginLoader, PluginRegistry
from notmyfault.security.security import SecurityMode


def scan(tmp_path, source):
    py_file = Path(tmp_path) / "action.py"
    py_file.write_text(source, encoding="utf-8")
    return scan_borrowed_privilege(str(py_file))


def test_plain_plugin_no_findings(tmp_path):
    assert scan(tmp_path, "def run(params):\n    return True\n") == []


def test_import_notmyfault_public_api_no_findings(tmp_path):
    source = (
        "import notmyfault\n"
        "import notmyfault.config\n"
        "from notmyfault.core.logging import engine_info\n"
    )
    assert scan(tmp_path, source) == []


def test_import_sudo_via_public_api_no_findings(tmp_path):
    source = "from notmyfault.security.sudo import run_as_admin\n"
    assert scan(tmp_path, source) == []


def test_direct_import_of_engine_plugin_module(tmp_path):
    findings = scan(tmp_path, "import notmyfault.action_other\n")
    assert any(
        "直接导入引擎插件模块 notmyfault.action_other" in f for f in findings
    )


def test_from_import_of_engine_plugin_module(tmp_path):
    findings = scan(tmp_path, "from notmyfault.trigger_hotkey import helper\n")
    assert any(
        "from-import 引擎插件模块 notmyfault.trigger_hotkey 的 helper" in f
        for f in findings
    )


def test_sys_modules_access(tmp_path):
    findings = scan(
        tmp_path, "import sys\nvictim = sys.modules['notmyfault.action_victim']\n"
    )
    assert any(
        "通过 sys.modules 获取引擎插件模块 notmyfault.action_victim" in f
        for f in findings
    )


def test_sys_modules_non_plugin_key_no_findings(tmp_path):
    assert scan(tmp_path, "import sys\nm = sys.modules['os']\n") == []


def test_sys_modules_getattr_bypass_detected(tmp_path):
    source = (
        "import sys\n"
        "run = getattr(sys.modules['notmyfault.action_victim'], 'run')\n"
    )
    findings = scan(tmp_path, source)
    assert any("sys.modules" in f for f in findings)


def test_sys_modules_subprocess_popen_bypass_detected(tmp_path):
    source = (
        "import sys\n"
        "m = sys.modules['notmyfault.trigger_x']\n"
        "m.Popen('cmd')\n"
    )
    findings = scan(tmp_path, source)
    assert any("sys.modules" in f for f in findings)
    assert any(
        "调用引擎插件模块内部函数 notmyfault.trigger_x.Popen()" in f
        for f in findings
    )


def test_alias_call_of_plugin_module_internal(tmp_path):
    source = (
        "import notmyfault.action_other as other\n"
        "other.helper()\n"
    )
    findings = scan(tmp_path, source)
    assert any(
        "调用引擎插件模块内部函数 notmyfault.action_other.helper()" in f
        for f in findings
    )


def test_getattr_builtins_exec_bypass(tmp_path):
    findings = scan(tmp_path, 'f = getattr(__builtins__, "exec")\n')
    assert any("getattr 获取内置动态执行函数 exec" in f for f in findings)


def test_getattr_unrelated_no_findings(tmp_path):
    assert scan(tmp_path, 'name = getattr(obj, "name")\n') == []


def test_builtins_subscript_exec_bypass_detected(tmp_path):
    py_file = Path(tmp_path) / "action.py"
    py_file.write_text('f = __builtins__["exec"]\nf("print(1)")\n', encoding="utf-8")
    assert "dynamic_exec" in scan_plugin_capabilities(str(py_file))


def test_builtins_subscript_benign_key_no_false_positive(tmp_path):
    py_file = Path(tmp_path) / "action.py"
    py_file.write_text('f = __builtins__["len"]\nf([1])\n', encoding="utf-8")
    assert "dynamic_exec" not in scan_plugin_capabilities(str(py_file))
    assert scan_borrowed_privilege(str(py_file)) == []


class SudoStub:
    def authorize_plugin(self, plugin_id, token, module=None):
        pass

    def deauthorize_plugin(self, plugin_id, token):
        pass


def test_loader_warns_on_borrowed_privilege(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        security_plugins, "_PLUGIN_MANIFEST_FILE", str(tmp_path / "manifest.json")
    )
    folder = tmp_path / "actions" / "borrow"
    folder.mkdir(parents=True)
    (folder / "action.json").write_text(
        json.dumps(
            {
                "id": "borrow",
                "name": "借壳插件",
                "description": "测试借壳扫描",
                "enabled": True,
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
        "def run(params):\n"
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
    )
    loaded, failed = loader.load(
        base_dir=str(tmp_path),
        plugins_dir="actions",
        json_filename="action.json",
        py_filename="action.py",
        module_prefix="notmyfault.action_",
        meta_store={},
        func_store={},
        store_name="Action",
        origin="user",
    )

    # 用户插件命中借壳扫描：告警并记录，但宽松模式下不拒载
    assert loaded == 1
    assert failed == 0
    err = capsys.readouterr().err
    assert "借壳提权嫌疑" in err
    assert any("borrow" in w for w in integrity_errors)
