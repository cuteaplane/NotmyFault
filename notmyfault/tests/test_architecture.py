import ast
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
EXISTING_IMPORTS = {
    "notmyfault.platform.platform_support": {"actions/notify/action.py"},
    "notmyfault.triggers.base": {
        f"triggers/{identity}/trigger.py" for identity in (
            "audio_device", "battery_level", "clipboard", "cron_schedule",
            "directory_quiescent", "file_content", "folder_monitor", "hotkey",
            "idle_detect", "network_status", "path_exists", "power_state",
            "process_state", "session_lock", "system_resource", "system_startup",
            "tcp_port", "time_schedule", "usb_insert", "wifi_network", "window_title",
        )
    },
}


def imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            yield node.module
            if node.module == "notmyfault.core" and any(
                alias.name in {"kernel", "Kernel"} for alias in node.names
            ):
                yield "notmyfault.core.kernel"


def test_builtin_plugins_use_public_api_without_new_private_imports():
    violations = []
    for kind in ("actions", "triggers"):
        for path in (PACKAGE / kind).glob("*/**/*.py"):
            relative = path.relative_to(PACKAGE).as_posix()
            for module in imports(path):
                if module != "notmyfault" and not module.startswith("notmyfault."):
                    continue
                if module == "notmyfault.plugin_api":
                    continue
                if relative not in EXISTING_IMPORTS.get(module, ()):
                    violations.append(f"{relative}: {module}")
    assert not violations, "内置插件导入了非公开模块:\n" + "\n".join(violations)


def test_core_does_not_import_kernel():
    violations = [
        path.name for path in (PACKAGE / "core").glob("*.py")
        if any(module == "notmyfault.core.kernel" for module in imports(path))
    ]
    assert not violations, f"核心组件不能导入 Kernel: {violations}"


def test_platform_does_not_import_plugin_api():
    violations = [
        path.relative_to(PACKAGE).as_posix()
        for path in (PACKAGE / "platform").rglob("*.py")
        if any(module == "notmyfault.plugin_api" for module in imports(path))
    ]
    assert not violations, f"平台层不能导入插件 API: {violations}"
