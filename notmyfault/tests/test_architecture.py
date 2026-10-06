import ast
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
EXISTING_IMPORTS = {
    "notmyfault.native": {
        "actions/document_quiescent/action.py", "actions/media_control/action.py",
        "actions/window_pin/action.py", "triggers/window_title/trigger.py",
    },
    "notmyfault.platform.backends": {"triggers/power_state/trigger.py"},
    "notmyfault.platform.linux_support": {
        "actions/lock_screen/action.py", "actions/screenshot/action.py",
        "actions/shutdown_system/action.py", "actions/text_to_speech/action.py",
        "actions/wallpaper/action.py", "triggers/idle_detect/trigger.py",
        "triggers/session_lock/trigger.py",
    },
    "notmyfault.platform.platform_support": {"actions/notify/action.py"},
    "notmyfault.platform.windows_wifi": {
        "actions/wifi_connect/action.py", "triggers/wifi_network/trigger.py",
    },
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
    assert not violations, "内置插件只能导入 notmyfault.plugin_api:\n" + "\n".join(violations)


def test_core_does_not_import_kernel():
    violations = [
        path.name for path in (PACKAGE / "core").glob("*.py")
        if any(module == "notmyfault.core.kernel" for module in imports(path))
    ]
    assert not violations, f"核心组件不能导入 Kernel: {violations}"
