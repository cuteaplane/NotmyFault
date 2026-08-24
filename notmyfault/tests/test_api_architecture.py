from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from notmyfault.host.api_server import ApiServer


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PACKAGE_ROOT / "host" / "api"
ROUTE_FILES = {
    "routes_engine.py",
    "routes_rules.py",
    "routes_ai.py",
    "routes_plugins.py",
    "routes_interactions.py",
    "routes_settings.py",
}


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(path: Path) -> set[str]:
    result = set()
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_business_modules_do_not_import_http_frameworks():
    files = list((API_ROOT / "services").glob("*.py"))
    files.extend((API_ROOT / name) for name in ("events.py", "plugin_installation.py"))
    violations = {}
    for path in files:
        imports = imported_modules(path)
        forbidden = sorted(
            name for name in imports if name == "fastapi" or name.startswith("starlette")
        )
        if forbidden:
            violations[path.name] = forbidden
    assert violations == {}


def test_routes_only_depend_on_api_services_and_http_types():
    assert {path.name for path in API_ROOT.glob("routes_*.py")} == ROUTE_FILES
    violations = {}
    for path in (API_ROOT / name for name in ROUTE_FILES):
        imports = imported_modules(path)
        forbidden = sorted(
            name
            for name in imports
            if name == "notmyfault.config"
            or name.startswith("notmyfault.core")
            or name.startswith("notmyfault.security")
        )
        if forbidden:
            violations[path.name] = forbidden
    assert violations == {}


def test_api_code_does_not_open_config_or_rules_files():
    violations = []
    for path in API_ROOT.rglob("*.py"):
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.id if isinstance(function, ast.Name) else ""
            if isinstance(function, ast.Attribute):
                name = function.attr
            if name not in {
                "open",
                "read_text",
                "read_bytes",
                "write_text",
                "write_bytes",
            }:
                continue
            expression = ast.unparse(node)
            if any(
                marker in expression
                for marker in (
                    "config_file",
                    "rules_file",
                    "config_path",
                    "rules_path",
                    "config.json",
                    "rules.json",
                )
            ):
                violations.append(f"{path.name}:{node.lineno}:{expression}")
    assert violations == []


def test_assembly_modules_define_no_endpoints():
    endpoint_methods = {"get", "post", "put", "patch", "delete", "options"}
    violations = []
    for path in (PACKAGE_ROOT / "host" / "api_server.py", API_ROOT / "application.py"):
        for node in ast.walk(parse(path)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    decorator = decorator.func
                if isinstance(decorator, ast.Attribute) and decorator.attr in endpoint_methods:
                    violations.append(f"{path.name}:{node.lineno}:{node.name}")
    assert violations == []


def test_api_server_has_only_the_public_runtime_surface():
    application = SimpleNamespace(app=object(), publish_event=lambda *args: None)
    server = ApiServer(application)
    public = {name for name in dir(server) if not name.startswith("_")}
    assert public == {"app", "publish_event", "serve", "stop"}


def test_removed_api_globals_and_legacy_class_do_not_return():
    production_files = [
        path
        for path in PACKAGE_ROOT.rglob("*.py")
        if "tests" not in path.parts and "__pycache__" not in path.parts
    ]
    names = {"EngineAPI", "CONFIG_FILE", "RULES_FILE", "_PLUGIN_MANIFEST_FILE"}
    violations = []
    for path in production_files:
        tree = parse(path)
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in names:
                    violations.append(f"{path}:{node.lineno}:{node.name}")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in names:
                        violations.append(f"{path}:{node.lineno}:{target.id}")
    assert violations == []
