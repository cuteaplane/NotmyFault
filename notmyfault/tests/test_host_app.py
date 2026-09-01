"""宿主启动文件检查"""

import pytest

from notmyfault.host import alert, app as host_app


def test_notify_build_required_calls_alert_user(monkeypatch):
    calls = []
    monkeypatch.setattr(
        alert, "alert_user",
        lambda title, message, open_dashboard=True: calls.append(
            (title, message, open_dashboard)
        ),
    )
    host_app._notify_build_required("缺少签名文件：build.json.sig")
    assert len(calls) == 1
    title, message, open_dashboard = calls[0]
    assert title == "NotmyFault 安装文件不完整"
    assert "拒绝启动" in message
    assert "build.json.sig" in message
    assert open_dashboard is False


def test_notify_build_required_survives_alert_failure(monkeypatch, capsys):
    def broken(title, message, open_dashboard=True):
        raise RuntimeError("通知组件不可用")

    monkeypatch.setattr(alert, "alert_user", broken)
    host_app._notify_build_required("缺少签名")
    assert "无法发送安装完整性提示" in capsys.readouterr().err


def _write_plugin(root, relative_path, json_name, *, signed):
    plugin_dir = root / relative_path
    plugin_dir.mkdir(parents=True)
    (plugin_dir / json_name).write_text("{}", encoding="utf-8")
    if signed:
        (plugin_dir / "signature.sig").write_bytes(b"signed")


def _prepare_startup_files(tmp_path, monkeypatch):
    pkg_root = tmp_path / "notmyfault"
    (tmp_path / "build.json").write_text("{}", encoding="utf-8")
    (tmp_path / "build.json.sig").write_bytes(b"signed")
    monkeypatch.setattr(host_app, "_PKG_ROOT", str(pkg_root))
    notices = []
    monkeypatch.setattr(host_app, "_notify_build_required", notices.append)
    return pkg_root, notices


def test_first_run_ignores_empty_removed_plugin_directories(tmp_path, monkeypatch):
    pkg_root, notices = _prepare_startup_files(tmp_path, monkeypatch)
    _write_plugin(pkg_root, "actions/notify", "action.json", signed=True)
    _write_plugin(pkg_root, "triggers/manual", "trigger.json", signed=True)
    _write_plugin(
        pkg_root,
        "actions/bluetooth_toggle",
        "action.json",
        signed=True,
    )
    (pkg_root / "actions/removed_action").mkdir(parents=True)
    (pkg_root / "triggers/removed_trigger").mkdir(parents=True)

    host_app._ensure_first_run_build()

    assert notices == []


def test_startup_rejects_unsigned_builtin_plugin(tmp_path, monkeypatch):
    pkg_root, notices = _prepare_startup_files(tmp_path, monkeypatch)
    _write_plugin(pkg_root, "actions/notify", "action.json", signed=True)
    _write_plugin(pkg_root, "triggers/manual", "trigger.json", signed=True)
    _write_plugin(
        pkg_root,
        "actions/bluetooth_toggle",
        "action.json",
        signed=False,
    )

    with pytest.raises(RuntimeError, match="bluetooth_toggle"):
        host_app._ensure_first_run_build()

    assert len(notices) == 1
    assert "bluetooth_toggle" in notices[0]


def test_startup_rejects_missing_build_signature(tmp_path, monkeypatch):
    pkg_root, notices = _prepare_startup_files(tmp_path, monkeypatch)
    (tmp_path / "build.json.sig").unlink()
    _write_plugin(pkg_root, "actions/notify", "action.json", signed=True)

    with pytest.raises(RuntimeError, match="build.json.sig"):
        host_app._ensure_first_run_build()

    assert len(notices) == 1
