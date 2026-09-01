import json
from pathlib import Path

import py7zr

from notmyfault import plugin_cli


def write_action(root: Path, **overrides) -> Path:
    root.mkdir(parents=True)
    meta = {
        "id": "sample_action",
        "name": "示例动作",
        "description": "命令行测试插件",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": "com.example.sample_action",
        "permissions": [],
    }
    meta.update(overrides)
    (root / "action.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (root / "action.py").write_text(
        "def run(action_info, params):\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    return root


def test_check_reports_all_plugin_surfaces(tmp_path):
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
    assert report["capabilities"] == []
    assert report["permissions"]["items"] == []
    assert report["risks"] == []
    assert report["signature"] == "none"
    assert report["entrypoints"]["selected"] == "action.py"
    assert set(report["contributions"]) == {
        "commands", "views", "parameter_editors", "data_types"
    }


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


def test_pack_writes_installable_nmfp(tmp_path):
    root = write_action(tmp_path / "packed")
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


def test_plugin_test_runs_pytest_in_plugin_directory(tmp_path):
    root = write_action(tmp_path / "tested")
    (root / "test_action.py").write_text(
        "def test_plugin_template():\n    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )

    result = plugin_cli.main(["plugin", "test", str(root), "-q"])

    assert result == 0


def test_create_action_and_trigger_templates(tmp_path):
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
        assert (root / "test_plugin.py").is_file()
        assert (root / ".github" / "workflows" / "test.yml").is_file()
        assert plugin_cli.main(["plugin", "test", str(root), "-q"]) == 0
        assert plugin_cli.main([
            "plugin", "create", kind, plugin_id, "--output-dir", str(tmp_path)
        ]) == 1
