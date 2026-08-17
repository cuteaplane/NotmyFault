"""宿主首次运行安全模式通知"""

from types import SimpleNamespace

from notmyfault.host import alert, app as host_app


def test_notify_first_run_mode_calls_alert_user(monkeypatch):
    calls = []
    monkeypatch.setattr(
        alert, "alert_user",
        lambda title, message, open_dashboard=True: calls.append(
            (title, message, open_dashboard)
        ),
    )
    host_app._notify_first_run_mode("permissive")
    assert len(calls) == 1
    title, message, open_dashboard = calls[0]
    assert title == "NotmyFault 首次运行"
    assert "permissive" in message
    assert open_dashboard is False


def test_notify_first_run_mode_survives_alert_failure(monkeypatch, capsys):
    def broken(title, message, open_dashboard=True):
        raise RuntimeError("通知组件不可用")

    monkeypatch.setattr(alert, "alert_user", broken)
    host_app._notify_first_run_mode("develop（normal）")
    assert "无法发送安全模式提示" in capsys.readouterr().err


def _write_plugin(root, relative_path, json_name, *, signed):
    plugin_dir = root / relative_path
    plugin_dir.mkdir(parents=True)
    (plugin_dir / json_name).write_text("{}", encoding="utf-8")
    if signed:
        (plugin_dir / "signature.sig").write_bytes(b"signed")


def _capture_first_run_build(tmp_path, monkeypatch):
    pkg_root = tmp_path / "notmyfault"
    (tmp_path / "build.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(host_app, "_PKG_ROOT", str(pkg_root))
    monkeypatch.setattr(host_app, "_notify_first_run_mode", lambda mode: None)
    calls = []
    monkeypatch.setattr(
        host_app,
        "_run_build_command",
        lambda cmd, build_dir: calls.append((cmd, build_dir))
        or SimpleNamespace(returncode=0, stderr=""),
    )
    return pkg_root, calls


def test_first_run_ignores_empty_removed_plugin_directories(tmp_path, monkeypatch):
    pkg_root, calls = _capture_first_run_build(tmp_path, monkeypatch)
    _write_plugin(pkg_root, "actions/notify", "action.json", signed=True)
    _write_plugin(pkg_root, "triggers/manual", "trigger.json", signed=True)
    _write_plugin(
        pkg_root,
        "bundled/actions/bluetooth_toggle",
        "action.json",
        signed=True,
    )
    (pkg_root / "actions/removed_action").mkdir(parents=True)
    (pkg_root / "triggers/removed_trigger").mkdir(parents=True)

    host_app._ensure_first_run_build()

    assert calls == []


def test_first_run_builds_when_bundled_plugin_is_unsigned(tmp_path, monkeypatch):
    pkg_root, calls = _capture_first_run_build(tmp_path, monkeypatch)
    _write_plugin(pkg_root, "actions/notify", "action.json", signed=True)
    _write_plugin(pkg_root, "triggers/manual", "trigger.json", signed=True)
    _write_plugin(
        pkg_root,
        "bundled/actions/bluetooth_toggle",
        "action.json",
        signed=False,
    )

    host_app._ensure_first_run_build()

    assert len(calls) == 1
