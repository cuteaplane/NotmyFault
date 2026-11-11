import json
from pathlib import Path

import py7zr
from notmyfault.security import signing

from notmyfault import plugin_cli


def write_action(root: Path, **overrides) -> Path:
    root.mkdir(parents=True)
    meta = {
        "id": "sample_action",
        "name": "示例动作",
        "description": "命令行测试插件",
        "version_code": 1,
        "version": "1.0",
        "package_name": "com.example.sample_action",
        "permissions": [],
        "execution_api": "context-v1",
    }
    meta.update(overrides)
    (root / "action.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (root / "action.py").write_text(
        "def run_with_context(action_info, params, context):\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    return root


def test_check_reports_all_plugin_surfaces(tmp_path, monkeypatch):
    from notmyfault.security.security import SecurityMode

    monkeypatch.setattr(plugin_cli, "detect_security_mode", lambda: SecurityMode.STRICT)
    root = write_action(
        tmp_path / "sample_action",
        contributes={
            "commands": [{
                "id": "capture",
                "title": "采集",
                "handler": "extension.py:capture",
            }],
        },
    )
    (root / "extension.py").write_text(
        "def capture(context, payload): return context.result()\n",
        encoding="utf-8",
    )

    report = plugin_cli.check_plugin(root)

    assert report["ok"] is True
    assert report["schema"] == {"ok": True, "errors": []}
    assert report["platform"]["compatible"] is True
    assert report["platform"]["checked"] is True
    assert "reason" not in report["platform"]
    assert report["capabilities"] == []
    assert report["permissions"]["items"] == []
    assert report["risks"] == []
    assert report["signature"] == "none"
    assert report["checks"]["development"]["allowed"] is True
    assert report["checks"]["load_policy"]["allowed"] is False
    assert "签名无效" in report["checks"]["load_policy"]["errors"]
    assert report["entrypoints"]["selected"] == "action.py"
    assert report["entrypoints"]["checked"] is True
    assert "reason" not in report["entrypoints"]
    assert set(report["contributions"]) == {
        "commands", "views", "parameter_editors", "data_types"
    }


def test_check_distinguishes_required_and_optional_capabilities(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(plugin_cli, "probe_capabilities", lambda: {
        "media.control": {
            "available": True, "backend": "playerctl", "reason": "", "degraded": False,
        },
        "audio.control": {
            "available": False, "backend": None, "reason": "未安装音量工具", "degraded": False,
        },
    })
    root = write_action(
        tmp_path / "optional",
        requires_capabilities=["media.control"],
        optional_capabilities=["audio.control"],
    )
    report = plugin_cli.check_plugin(root)
    assert report["ok"] is True
    assert [(item["id"], item["required"]) for item in report["capabilities"]] == [
        ("media.control", True), ("audio.control", False),
    ]
    plugin_cli._print_report(report)
    text = capsys.readouterr().out
    assert "capability: media.control (必需) ok" in text
    assert "capability: audio.control (可选) 未安装音量工具" in text


def test_check_handles_malformed_manifest(tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    (root / "action.json").write_text("{broken", encoding="utf-8")

    report = plugin_cli.check_plugin(root)

    assert report["ok"] is False
    assert report["errors"] == ["插件清单无法读取或不是有效 JSON"]


def test_check_reports_ast_risks(tmp_path):
    root = write_action(tmp_path / "risky")
    (root / "action.py").write_text(
        "import importlib\nimportlib.import_module('os')\n",
        encoding="utf-8",
    )

    report = plugin_cli.check_plugin(root)

    assert any(risk["id"] == "dynamic_import" for risk in report["risks"])


def test_check_text_report_explains_development_errors_and_unchecked_checks(tmp_path, capsys):
    missing = write_action(tmp_path / "missing_permission")
    (missing / "action.py").write_text(
        "import subprocess\n\n\n"
        "def run_with_context(action_info, params, context):\n"
        "    subprocess.run(['true'], check=False)\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )

    result = plugin_cli.main(["plugin", "check", str(missing)])
    captured = capsys.readouterr()
    assert result == 1
    assert "开发检查错误: 未声明能力，使用了未在清单声明的能力: external_binary" in captured.out

    pack_result = plugin_cli.main([
        "plugin", "pack", str(missing), "--output-dir", str(tmp_path / "output")
    ])
    pack_captured = capsys.readouterr()
    assert pack_result == 1
    assert "开发检查错误: 未声明能力，使用了未在清单声明的能力: external_binary" in pack_captured.out

    invalid = write_action(tmp_path / "invalid_outputs", outputs=["result"])
    report = plugin_cli.check_plugin(invalid)
    assert report["platform"]["checked"] is False
    assert report["platform"]["compatible"] is False
    assert report["platform"]["reason"] == "schema 未通过"
    assert report["entrypoints"]["checked"] is False
    assert report["entrypoints"]["selected"] is None
    assert report["entrypoints"]["reason"] == "schema 未通过"

    result = plugin_cli.main(["plugin", "check", str(invalid)])
    captured = capsys.readouterr()
    assert result == 1
    assert "platform: 未检查（schema 未通过）" in captured.out
    assert "entrypoint: 未检查（schema 未通过）" in captured.out
    assert "platform: linux unavailable" not in captured.out
    assert "entrypoint: none missing" not in captured.out


def test_pack_writes_installable_nmfp(tmp_path):
    root = write_action(tmp_path / "packed")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signing.self_sign_plugin(root, Ed25519PrivateKey.generate())
    output = tmp_path / "output"

    result = plugin_cli.main([
        "plugin", "pack", str(root), "--output-dir", str(output)
    ])

    archive = output / "sample_action.nmfp"
    assert result == 0
    assert archive.is_file()
    with py7zr.SevenZipFile(archive) as package:
        assert "sample_action/action.json" in package.getnames()
        assert "sample_action/action.py" in package.getnames()
        package.extractall(tmp_path / "unpacked")
    from notmyfault.security.plugins import plugin_signature_kind

    assert plugin_signature_kind(str(tmp_path / "unpacked" / "sample_action"), "user") == "author"


def test_plugin_test_runs_pytest_in_plugin_directory(tmp_path, capsys):
    root = write_action(tmp_path / "tested")
    (root / "test_action.py").write_text(
        "def test_plugin_template():\n    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )

    result = plugin_cli.main(["plugin", "test", str(root), "-q"])

    captured = capsys.readouterr()
    assert result == 0, captured.out + captured.err
    assert "1 passed" in captured.out


def test_create_action_and_trigger_templates(tmp_path, capsys):
    for kind in ("action", "trigger"):
        plugin_id = f"sample_{kind}"
        result = plugin_cli.main([
            "plugin",
            "create",
            kind,
            plugin_id,
            "--output-dir",
            str(tmp_path),
        ])
        root = tmp_path / plugin_id
        assert result == 0
        assert plugin_cli.check_plugin(root)["ok"] is True
        if kind == "trigger":
            meta = json.loads((root / "trigger.json").read_text(encoding="utf-8"))
            assert meta["trigger_api"] == "event-v2"
        else:
            meta = json.loads((root / "action.json").read_text(encoding="utf-8"))
            assert meta["execution_api"] == "context-v1"
            assert "def run_with_context(action_info, params, context)" in (root / "action.py").read_text()
        assert (root / "test_plugin.py").is_file()
        assert (root / ".github" / "workflows" / "test.yml").is_file()
        test_result = plugin_cli.main(["plugin", "test", str(root), "-q"])
        captured = capsys.readouterr()
        assert test_result == 0, captured.out + captured.err
        assert plugin_cli.main([
            "plugin", "create", kind, plugin_id, "--output-dir", str(tmp_path)
        ]) == 1
