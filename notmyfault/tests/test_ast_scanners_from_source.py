"""源码版扫描与载荷版验签：路径版结果一致，一次解析等于三次独立扫描"""

import ast
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from notmyfault.security import plugins as security_plugins
from notmyfault.security import signing
from notmyfault.security import signing_keys
from notmyfault.security.plugins import (
    analyze_plugin_source,
    check_sudo_import,
    check_sudo_import_from_source,
    compute_file_hash,
    parse_plugin_source,
    plugin_signature_kind,
    plugin_signature_kind_from_payload,
    scan_borrowed_privilege,
    scan_borrowed_privilege_from_source,
    scan_plugin_capabilities,
    scan_plugin_capabilities_from_source,
    verify_plugin_integrity,
    verify_plugin_integrity_from_hashes,
)

# 覆盖三条扫描规则的全部分支，含语法错误的坏源码。
SCAN_SAMPLES = [
    "def run(params):\n    return True\n",
    "",
    "import notmyfault.security.sudo\n",
    "import notmyfault.security.sudo as s\n",
    "import notmyfault.security as sec\n",
    "from notmyfault import sudo\n",
    "from notmyfault import config, sudo\n",
    "from notmyfault.security import sudo\n",
    "from notmyfault.security.sudo import run_as_admin\n",
    "import notmyfault.config\n",
    "import os\nimport sys\n",
    "# import notmyfault.security.sudo\n",
    "text = 'import notmyfault.security.sudo'\n",
    "import subprocess\n",
    "import subprocess\nsubprocess.run(['dir'])\n",
    "import subprocess\np = subprocess.Popen(['x'])\n",
    "import subprocess\nsubprocess.getoutput('x')\n",
    "import subprocess as sb\nsb.check_output(['x'])\n",
    "from subprocess import run\nrun(['dir'])\n",
    "from subprocess import call as c\nc(['x'])\n",
    "import subprocess\nsubprocess.run('dir', shell=True)\n",
    "launch('dir', shell=True)\n",
    "launch('dir', shell=False)\n",
    "import os\nos.system('dir')\n",
    "import os\nos.popen('dir')\n",
    "import os\nos.execvp('x', [])\n",
    "import os as system\nsystem.startfile('a.txt')\n",
    "from os import system\nsystem('dir')\n",
    "from os import path\npath.join('a', 'b')\n",
    "import ctypes\n",
    "import ctypes\nctypes.windll.user32\n",
    "import ctypes\nctypes.CDLL('x.dll')\n",
    "import ctypes\nctypes.WINFUNCTYPE(None)\n",
    "from ctypes import windll\n",
    "from ctypes import WinDLL\nWinDLL('x')\n",
    "from ctypes import Structure\nclass S(Structure):\n    pass\n",
    "import win32api\n",
    "import win32con\nShellExecuteEx(...)\n",
    "from win32api import ShellExecuteW\nShellExecuteW(1, 2, 3, 4, 5, 6)\n",
    "verb = 'runas'\n",
    "verb = 'not runas'\n",
    "eval('1')\n",
    "exec('x = 1')\n",
    "compile('1', '<s>', 'eval')\n",
    "__import__('os')\n",
    "__import__('ctypes')\n",
    "import builtins\nbuiltins.eval('1')\n",
    "import builtins\ngetattr(builtins, 'exec')\n",
    "f = getattr(__builtins__, 'exec')\n",
    "f = __builtins__['exec']\n",
    "f = __builtins__['len']\n",
    "name = getattr(obj, 'name')\n",
    "import importlib\nimportlib.import_module('os')\n",
    "import importlib\nimportlib.import_module('ctypes')\n",
    "import importlib as il\nil.import_module(name)\n",
    "import sys\ngetattr(sys.modules['os'], 'system')\n",
    "import sys\nm = sys.modules['subprocess']\nm.Popen('cmd')\n",
    "import sys\nm = sys.modules['ctypes']\nm.windll\n",
    "import sys\nm = sys.modules['builtins']\nm.exec('x=1')\n",
    "import sys\nm = sys.modules['os']\n",
    "import notmyfault.action_other\n",
    "import notmyfault.action_other as other\nother.helper()\n",
    "from notmyfault.trigger_hotkey import helper\n",
    "import sys\nvictim = sys.modules['notmyfault.action_victim']\n",
    "import sys\nrun = getattr(sys.modules['notmyfault.action_victim'], 'run')\n",
    "import sys\nm = sys.modules['notmyfault.trigger_x']\nm.Popen('cmd')\n",
    "from notmyfault.core.logging import engine_info\n",
    "def (\n",
]


def _write(tmp_path, source):
    py_file = Path(tmp_path) / "action.py"
    py_file.write_text(source, encoding="utf-8")
    return str(py_file)


class TestParsePluginSource:
    def test_valid_source_returns_module(self):
        tree = parse_plugin_source("x = 1\n")
        assert isinstance(tree, ast.Module)
        assert tree.body

    def test_bad_source_returns_empty_module(self):
        tree = parse_plugin_source("def (\n")
        assert isinstance(tree, ast.Module)
        assert tree.body == []

    def test_empty_source_returns_empty_module(self):
        assert parse_plugin_source("").body == []


class TestSudoFromSource:
    @pytest.mark.parametrize(
        "source, expected",
        [
            ("import notmyfault.security.sudo\n", True),
            ("import notmyfault.security as sec\n", True),
            ("from notmyfault.security import sudo\n", True),
            ("import notmyfault.config\n", False),
            ("text = 'import notmyfault.security.sudo'\n", False),
            ("def (\n", False),
            ("", False),
        ],
    )
    def test_expected_values(self, source, expected):
        assert check_sudo_import_from_source(source) is expected


class TestCapabilitiesFromSource:
    @pytest.mark.parametrize(
        "source, expected",
        [
            ("def run(p):\n    return True\n", set()),
            ("import subprocess\n", {"external_binary"}),
            ("import ctypes\n", {"native_api"}),
            ("eval('1')\n", {"dynamic_exec"}),
            ("launch('dir', shell=True)\n", {"external_binary"}),
            ("verb = 'runas'\n", {"self_elevation"}),
            ("import os\nos.system('x')\n", {"external_binary"}),
            ("from ctypes import windll\n", {"native_api"}),
            (
                "import subprocess\nimport ctypes\neval('1')\n"
                "subprocess.run('x', shell=True)\nverb = 'runas'\n",
                {"external_binary", "native_api", "dynamic_exec", "self_elevation"},
            ),
            ("def (\n", set()),
            ("", set()),
        ],
    )
    def test_expected_values(self, source, expected):
        assert scan_plugin_capabilities_from_source(source) == expected


class TestBorrowedFromSource:
    @pytest.mark.parametrize(
        "source, fragment",
        [
            (
                "import notmyfault.action_other\n",
                "直接导入引擎插件模块 notmyfault.action_other",
            ),
            (
                "from notmyfault.trigger_hotkey import helper\n",
                "from-import 引擎插件模块 notmyfault.trigger_hotkey",
            ),
            (
                "import sys\nvictim = sys.modules['notmyfault.action_victim']\n",
                "通过 sys.modules 获取引擎插件模块 notmyfault.action_victim",
            ),
            ("f = getattr(__builtins__, 'exec')\n", "getattr 获取内置动态执行函数 exec"),
            (
                "import notmyfault.action_other as other\nother.helper()\n",
                "调用引擎插件模块内部函数 notmyfault.action_other.helper()",
            ),
        ],
    )
    def test_findings(self, source, fragment):
        findings = scan_borrowed_privilege_from_source(source)
        assert any(fragment in f for f in findings)

    @pytest.mark.parametrize(
        "source",
        [
            "def run(p):\n    return True\n",
            "import sys\nm = sys.modules['os']\n",
            "name = getattr(obj, 'name')\n",
            "from notmyfault.core.logging import engine_info\n",
            "from notmyfault.security.sudo import run_as_admin\n",
        ],
    )
    def test_clean_sources(self, source):
        assert scan_borrowed_privilege_from_source(source) == []


class TestPathSourceParity:
    @pytest.mark.parametrize("source", SCAN_SAMPLES)
    def test_sudo_parity(self, tmp_path, source):
        assert check_sudo_import(_write(tmp_path, source)) is check_sudo_import_from_source(
            source
        )

    @pytest.mark.parametrize("source", SCAN_SAMPLES)
    def test_capabilities_parity(self, tmp_path, source):
        assert scan_plugin_capabilities(
            _write(tmp_path, source)
        ) == scan_plugin_capabilities_from_source(source)

    @pytest.mark.parametrize("source", SCAN_SAMPLES)
    def test_borrowed_parity(self, tmp_path, source):
        assert scan_borrowed_privilege(
            _write(tmp_path, source)
        ) == scan_borrowed_privilege_from_source(source)


class TestAnalyzePluginSource:
    @pytest.mark.parametrize("source", SCAN_SAMPLES)
    def test_equals_three_independent_scanners(self, source):
        assert analyze_plugin_source(source) == (
            scan_plugin_capabilities_from_source(source),
            check_sudo_import_from_source(source),
            scan_borrowed_privilege_from_source(source),
        )

    def test_single_ast_parse(self, monkeypatch):
        real_parse = security_plugins.ast.parse
        calls = []

        def counting_parse(source):
            calls.append(1)
            return real_parse(source)

        monkeypatch.setattr(security_plugins.ast, "parse", counting_parse)
        analyze_plugin_source("import subprocess\nimport notmyfault.security.sudo\n")
        assert len(calls) == 1


class TestSignatureKindFromPayload:
    def _plugin_dir(self, tmp_path):
        folder = tmp_path / "actions" / "plug"
        folder.mkdir(parents=True)
        (folder / "action.json").write_text('{"id": "plug"}', encoding="utf-8")
        (folder / "action.py").write_text(
            "def run(m, p):\n    pass\n", encoding="utf-8"
        )
        return folder

    @staticmethod
    def _payload(folder):
        return b"".join(f.read_bytes() for f in signing.plugin_files(folder))

    def test_official_parity(self, tmp_path, monkeypatch):
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [pub])
        folder = self._plugin_dir(tmp_path)
        signing.sign_plugin(folder, "action.json", private_key=key)
        payload = self._payload(folder)
        assert plugin_signature_kind_from_payload(str(folder), "builtin", payload) == "official"
        assert plugin_signature_kind_from_payload(
            str(folder), "builtin", payload
        ) == plugin_signature_kind(str(folder), "builtin")

    def test_author_parity(self, tmp_path, monkeypatch):
        author_key = Ed25519PrivateKey.generate()
        user_key = Ed25519PrivateKey.generate()
        user_pub = user_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [user_pub])
        folder = self._plugin_dir(tmp_path)
        signing.self_sign_plugin(folder, author_key)
        signing.counter_sign_author_key(folder, user_key)
        payload = self._payload(folder)
        assert plugin_signature_kind_from_payload(str(folder), "user", payload) == "author"
        assert plugin_signature_kind_from_payload(
            str(folder), "user", payload
        ) == plugin_signature_kind(str(folder), "user")

    def test_tampered_payload_is_none(self, tmp_path, monkeypatch):
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [pub])
        folder = self._plugin_dir(tmp_path)
        signing.sign_plugin(folder, "action.json", private_key=key)
        assert (
            plugin_signature_kind_from_payload(str(folder), "builtin", b"tampered")
            == "none"
        )

    def test_unsigned_plugin_is_none(self, tmp_path):
        folder = self._plugin_dir(tmp_path)
        assert (
            plugin_signature_kind_from_payload(
                str(folder), "user", self._payload(folder)
            )
            == "none"
        )

    def test_verify_sig_with_payload(self):
        key = Ed25519PrivateKey.generate()
        payload = b"hello payload"
        sig = key.sign(hashlib.sha256(payload).digest())
        assert security_plugins._verify_sig_with_payload(
            sig, payload, [key.public_key()]
        )
        assert not security_plugins._verify_sig_with_payload(
            sig, b"other payload", [key.public_key()]
        )
        assert not security_plugins._verify_sig_with_payload(sig, payload, [])


class TestIntegrityFromHashes:
    def test_parity_with_path_api(self, tmp_path, monkeypatch):
        manifest_path = tmp_path / "config" / "plugin_manifest.json"
        monkeypatch.setattr(
            security_plugins, "_PLUGIN_MANIFEST_FILE", str(manifest_path)
        )
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "action.json").write_text("{}", encoding="utf-8")
        (plugin_dir / "action.py").write_text("def run(): pass\n", encoding="utf-8")
        files = [(p.name, str(p)) for p in sorted(plugin_dir.iterdir()) if p.is_file()]

        ok_path, msg_path = verify_plugin_integrity("demo", files)
        hashes = {name: compute_file_hash(path) for name, path in files}
        ok_hashes, msg_hashes = verify_plugin_integrity_from_hashes("demo", hashes)
        assert ok_path is ok_hashes is True
        assert msg_path == msg_hashes == "完整性校验通过"

        # 文件改动后两个入口给出一致的失败消息。
        (plugin_dir / "action.py").write_text(
            "def run():\n    return 1\n", encoding="utf-8"
        )
        ok_path, msg_path = verify_plugin_integrity("demo", files)
        hashes = {name: compute_file_hash(path) for name, path in files}
        ok_hashes, msg_hashes = verify_plugin_integrity_from_hashes("demo", hashes)
        assert ok_path is ok_hashes is False
        assert msg_path == msg_hashes
        assert "action.py 文件已被修改" in msg_hashes
