from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from notmyfault.platform.capabilities import probe_capabilities
from notmyfault.security.plugin_schema import (
    check_permissions_conform,
    current_platform_name,
    get_permission_info,
    validate_plugin_meta,
)
from notmyfault.security.plugin_checks import inspect_plugin, inspection_report
from notmyfault.security.plugins import load_plugin_manifest
from notmyfault.security.security import detect_security_mode


def _manifest_path(plugin_dir: Path) -> tuple[Path | None, str | None]:
    for filename, plugin_type in (
        ("action.json", "action"),
        ("trigger.json", "trigger"),
    ):
        path = plugin_dir / filename
        if path.is_file():
            return path, plugin_type
    return None, None


def check_plugin(plugin_dir: str | Path) -> dict[str, Any]:
    root = Path(plugin_dir).resolve()
    report: dict[str, Any] = {"path": str(root), "ok": False}
    manifest_path, plugin_type = _manifest_path(root)
    if manifest_path is None or plugin_type is None:
        report["errors"] = ["未找到 action.json 或 trigger.json"]
        return report
    capability_report = probe_capabilities()
    inspection = inspect_plugin(root, plugin_type, capability_report=capability_report, installed_manifest=load_plugin_manifest())
    if inspection.errors:
        report["errors"] = ["插件清单无法读取或不是有效 JSON"] if any(error.startswith("JSON 解析失败") for error in inspection.errors) else inspection.errors
        return report
    meta = inspection.meta
    schema_errors = inspection.schema_errors
    schema_ok = not schema_errors
    platform = current_platform_name()
    platform_ok = inspection.platform_compatible
    required = meta.get("requires_capabilities") or []
    if not isinstance(required, list):
        required = []
    capabilities = [
        {"id": capability, **capability_report.get(capability, {
            "available": False,
            "backend": None,
            "reason": "未知能力 id",
            "degraded": False,
        })}
        for capability in required
    ]
    permissions = meta.get("permissions") or []
    if not isinstance(permissions, list):
        permissions = []
    permission_ok, permission_errors = check_permissions_conform(permissions)
    permission_report = []
    for permission in permissions:
        info = get_permission_info(permission)
        permission_report.append({
            "id": permission,
            "known": info is not None,
            "risk": info["risk"] if info else "unknown",
        })
    checks = inspection_report(inspection, detect_security_mode())
    selected = Path(inspection.entrypoint).relative_to(root).as_posix() if inspection.entrypoint else None
    entrypoint = {
        "platform": platform,
        "declared": meta.get("entrypoints") or {},
        "selected": selected,
        "exists": inspection.entrypoint_exists,
    }
    contributes = meta.get("contributes") or {}
    if not isinstance(contributes, dict):
        contributes = {}
    contribution_ids = {}
    for kind in ("commands", "views", "parameter_editors", "data_types"):
        items = contributes.get(kind) or []
        if not isinstance(items, list):
            items = []
        contribution_ids[kind] = [
            item.get("id") for item in items if isinstance(item, dict)
        ]
    report.update({
        "type": plugin_type,
        "id": meta.get("id", ""),
        "package_name": meta.get("package_name", ""),
        "schema": {"ok": schema_ok, "errors": schema_errors},
        "platform": {"name": platform, "compatible": platform_ok},
        "capabilities": capabilities,
        "permissions": {
            "ok": permission_ok,
            "errors": permission_errors,
            "items": permission_report,
        },
        "risks": inspection.risks,
        "borrowed_privilege": sorted(set(inspection.borrowed)),
        "signature": inspection.signature.kind,
        "checks": checks,
        "entrypoints": entrypoint,
        "contributions": contribution_ids,
    })
    report["ok"] = checks["development"]["allowed"]
    return report


def _print_report(report: dict[str, Any]) -> None:
    state = "通过" if report.get("ok") else "未通过"
    print(f"plugin check 开发检查: {state}")
    print(f"路径: {report.get('path', '')}")
    if "type" not in report:
        for error in report.get("errors", []):
            print(f"错误: {error}")
        return
    print(
        f"插件: {report.get('type')} {report.get('id')} "
        f"({report.get('package_name')})"
    )
    print(f"schema: {'ok' if report['schema']['ok'] else 'failed'}")
    for error in report["schema"]["errors"]:
        print(f"  - {error}")
    print(
        f"platform: {report['platform']['name']} "
        f"{'ok' if report['platform']['compatible'] else 'unavailable'}"
    )
    for item in report["capabilities"]:
        state = "ok" if item["available"] else item.get("reason") or "unavailable"
        print(f"capability: {item['id']} {state}")
    for item in report["permissions"]["items"]:
        print(f"permission: {item['id']} {item['risk']}")
    print(f"signature: {report['signature']}")
    policy = report["checks"]["load_policy"]
    print(f"load policy ({policy['mode']}): {'allowed' if policy['allowed'] else 'rejected'}")
    for error in policy["errors"]:
        print(f"  - {error}")
    selected = report["entrypoints"].get("selected") or "none"
    print(f"entrypoint: {selected} {'ok' if report['entrypoints']['exists'] else 'missing'}")
    for kind, identifiers in report["contributions"].items():
        print(f"contributions.{kind}: {', '.join(identifiers) or 'none'}")
    for risk in report["risks"]:
        print(f"risk: {risk.get('id')} {risk.get('file', '')}")
    for finding in report["borrowed_privilege"]:
        print(f"borrowed_privilege: {finding}")


def _run_tests(plugin_dir: Path, extra_args: list[str]) -> int:
    command = [sys.executable, "-m", "pytest", str(plugin_dir), *extra_args]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("plugin test 超过 300 秒，已停止等待", file=sys.stderr)
        return 2
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def _pack(plugin_dir: Path, output_dir: Path | None) -> int:
    report = check_plugin(plugin_dir)
    if not report.get("ok"):
        _print_report(report)
        return 1
    import pack_plugin

    result = pack_plugin.pack_plugin(
        plugin_dir,
        output_dir=output_dir or pack_plugin.DIST_DIR,
        arc_prefix=report["id"],
    )
    return 0 if result else 1


def _create_plugin(
    kind: str,
    plugin_id: str,
    output_dir: Path,
    package_name: str | None,
) -> int:
    root = output_dir / plugin_id
    if root.exists():
        print(f"目录已经存在，不会覆盖: {root}", file=sys.stderr)
        return 1
    package_name = package_name or f"com.example.{plugin_id}"
    filename = "action.json" if kind == "action" else "trigger.json"
    source_name = "action.py" if kind == "action" else "trigger.py"
    meta: dict[str, Any] = {
        "id": plugin_id,
        "name": plugin_id.replace("_", " ").title(),
        "description": "请填写插件说明",
        "enabled": True,
        "version_code": 1,
        "version": "0.1.0",
        "package_name": package_name,
        "permissions": [],
        "params": [],
    }
    if kind == "trigger":
        meta.update({"semantic": "oneshot", "trigger_api": "event-v2"})
        source = (
            "def run(meta, config, emit_event, shutdown_event):\n"
            "    shutdown_event.wait()\n"
        )
        test_source = (
            "import trigger\n\n\n"
            "def test_trigger_entrypoint_exists():\n"
            "    assert callable(trigger.run)\n"
        )
    else:
        source = (
            "def run(action_info, params):\n"
            "    return {\"ok\": True}\n"
        )
        test_source = (
            "from action import run\n\n\n"
            "def test_action_runs():\n"
            "    assert run({}, {}) == {\"ok\": True}\n"
        )
    valid, errors = validate_plugin_meta(meta, kind)
    if not valid:
        print("无法创建插件模板: " + "; ".join(errors[:5]), file=sys.stderr)
        return 1
    workflow = """name: plugin-tests
on: [push, pull_request]
jobs:
  test:
    strategy:
      matrix:
        os: [windows-latest, ubuntu-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: python -m pip install pytest
      - run: python -m pytest -q
"""
    root.mkdir(parents=True)
    (root / filename).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (root / source_name).write_text(source, encoding="utf-8")
    (root / "test_plugin.py").write_text(test_source, encoding="utf-8")
    workflow_path = root / ".github" / "workflows" / "test.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(workflow, encoding="utf-8")
    print(f"已创建 {kind} 插件模板: {root}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nmf")
    root = parser.add_subparsers(dest="group", required=True)
    plugin = root.add_parser("plugin", help="检查、测试或打包插件")
    commands = plugin.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("path")
    check.add_argument("--json", action="store_true", dest="as_json")
    test = commands.add_parser("test")
    test.add_argument("path")
    test.add_argument("pytest_args", nargs=argparse.REMAINDER)
    pack = commands.add_parser("pack")
    pack.add_argument("path")
    pack.add_argument("--output-dir")
    create = commands.add_parser("create")
    create.add_argument("kind", choices=("action", "trigger"))
    create.add_argument("plugin_id")
    create.add_argument("--output-dir", default=".")
    create.add_argument("--package-name")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "create":
        return _create_plugin(
            args.kind,
            args.plugin_id,
            Path(args.output_dir).resolve(),
            args.package_name,
        )
    plugin_dir = Path(args.path).resolve()
    if args.command == "check":
        report = check_plugin(plugin_dir)
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_report(report)
        return 0 if report.get("ok") else 1
    if args.command == "test":
        return _run_tests(plugin_dir, args.pytest_args)
    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    return _pack(plugin_dir, output_dir)
