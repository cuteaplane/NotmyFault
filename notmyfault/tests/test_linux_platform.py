"""可在任意宿主上执行的 Linux 平台适配回归。"""

from notmyfault.platform import platform_support
from notmyfault.security import plugin_loader


def test_linux_paths_and_python_launch_use_xdg_and_current_interpreter(
    tmp_path, monkeypatch
):
    launched = []
    monkeypatch.setattr(platform_support.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        platform_support.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append((command, kwargs)),
    )

    assert platform_support.get_config_dir() == f"{tmp_path}/notmyfault"
    platform_support.launch_python_entry("/opt/notmyfault/dashboard.pyw")
    assert launched[0][0][1] == "/opt/notmyfault/dashboard.pyw"
    assert launched[0][1]["start_new_session"] is True


def test_linux_autostart_uses_xdg_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_support.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    platform_support.set_linux_autostart(True, "/opt/notmyfault")
    desktop_file = platform_support.linux_autostart_path()
    content = desktop_file.read_text(encoding="utf-8")
    assert desktop_file == tmp_path / "autostart" / "notmyfault.desktop"
    assert '"/opt/notmyfault/NOTMYFAULT.pyw"' in content

    platform_support.set_linux_autostart(False, "/opt/notmyfault")
    assert not desktop_file.exists()


def test_linux_plugin_compatibility_and_entrypoint(monkeypatch):
    monkeypatch.setattr(plugin_loader.sys, "platform", "linux")
    assert not plugin_loader.is_plugin_platform_compatible(
        {"platforms": ["windows"]}
    )
    assert plugin_loader.is_plugin_platform_compatible(
        {"platforms": ["windows", "linux"]}
    )
    assert plugin_loader.resolve_plugin_entrypoint(
        "/tmp/example",
        {"entrypoints": {"windows": "windows/action.py", "linux": "linux/action.py"}},
        "action.py",
    ) == "/tmp/example/linux/action.py"
