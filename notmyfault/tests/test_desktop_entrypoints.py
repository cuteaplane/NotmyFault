import importlib.machinery
import importlib.util
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.request

import pytest

from notmyfault.application_paths import ApplicationPaths


@pytest.fixture
def dashboard(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "dashboard.pyw"
    loader = importlib.machinery.SourceFileLoader("desktop_dashboard_test", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    return module


def test_dashboard_get_config_does_not_rewrite_rules_file(dashboard, tmp_path):
    import hashlib
    import hmac
    import json

    from notmyfault.core.value_codec import encode_value
    from notmyfault.tests.api_support import make_paths, make_store

    paths = make_paths(tmp_path)
    store = make_store(paths)
    rule = {
        "name": "提醒",
        "event": {"type": "time_schedule", "params": {"time": "08:00"}},
        "actions": [{"type": "notify", "params": {"title": "提醒"}}],
    }
    payload = {
        "schema_version": 2,
        "value_encoding": "typed-v1",
        "rules": encode_value([rule]),
    }
    secret = paths.config_secret_file.read_bytes()
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    payload["_signature"] = hmac.new(
        secret, content.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    paths.rules_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before = paths.rules_file.read_bytes()
    result = dashboard.DashboardAPI(store=store, paths=paths).get_config()
    assert "_error" not in result
    assert result["rules"]
    assert paths.rules_file.read_bytes() == before


def test_dashboard_save_config_writes_signed_rules(dashboard, tmp_path):
    from notmyfault.core.value_codec import encode_value
    from notmyfault.tests.api_support import make_paths, make_store

    paths = make_paths(tmp_path)
    store = make_store(paths)
    api = dashboard.DashboardAPI(store=store, paths=paths)
    rule = {
        "name": "提醒",
        "event": {"type": "time_schedule", "params": {"time": "08:00"}},
        "actions": [{"type": "notify", "params": {"title": "提醒"}}],
    }
    result = api.save_config(encode_value([rule]), "", "typed-v1")
    assert result.get("ok") is True, result
    loaded = store.load_verified_rules()
    assert loaded[0]["name"] == "提醒"
    assert loaded[0]["actions"][0]["type"] == "notify"


def test_dashboard_reads_history_from_application_paths(dashboard, tmp_path):
    paths = ApplicationPaths(tmp_path / "config", tmp_path / "package", tmp_path)
    paths.logs_dir.mkdir(parents=True)
    (paths.logs_dir / "engine-20260906.log").write_text(
        "[2026-09-06 12:00:00] [INFO] 桌面日志\n", encoding="utf-8"
    )
    api = dashboard.DashboardAPI(paths=paths)
    assert api.list_log_files()[0]["name"] == "engine-20260906.log"
    assert api.read_log_file_entries("engine-20260906.log")[0]["text"] == "桌面日志"


def test_dashboard_rebuilds_changed_sources_and_rejects_failed_build(dashboard, tmp_path, monkeypatch):
    root = tmp_path / "dashboard"
    (root / "dist").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "package.json").write_text("{}", encoding="utf-8")
    source = root / "src" / "main.js"
    source.write_text("export default 1", encoding="utf-8")
    index = root / "dist" / "index.html"
    index.write_text("built", encoding="utf-8")
    timestamp = time.time_ns()
    os.utime(index, ns=(timestamp, timestamp + 1_000_000_000))
    monkeypatch.setattr(dashboard, "PROJECT_ROOT", str(tmp_path))
    calls = []

    def build(*args, **kwargs):
        calls.append(args)
        os.utime(index, ns=(timestamp, timestamp + 3_000_000_000))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", build)
    assert dashboard._ensure_dashboard_build() is True
    assert calls == []
    os.utime(source, ns=(timestamp, timestamp + 2_000_000_000))
    assert dashboard._ensure_dashboard_build() is True
    assert len(calls) == 1
    os.utime(source, ns=(timestamp, timestamp + 4_000_000_000))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="build failed"))
    assert dashboard._resolve_dashboard_url() == (None, None)


def test_static_server_handles_parallel_requests_and_caches_hashed_assets(dashboard, tmp_path):
    (tmp_path / "index.html").write_text("dashboard", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "main-a1b2c3d4.js").write_text("asset", encoding="utf-8")
    server, _url = dashboard._start_static_server(str(tmp_path), port=0)
    idle = socket.create_connection(server.server_address, timeout=2)
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        for path, expected in (("/", "no-store"), ("/assets/main-a1b2c3d4.js", "immutable")):
            with urllib.request.urlopen(origin + path, timeout=2) as response:
                assert expected in response.headers["Cache-Control"]
                assert response.read()
    finally:
        idle.close()
        server.shutdown()
        server.server_close()


def test_media_permissions_require_local_page(dashboard, monkeypatch):
    decisions = []
    feature = SimpleNamespace(MediaAudioCapture=1, MediaVideoCapture=2,
                              MediaAudioVideoCapture=3, ClipboardReadWrite=4)
    page = type("Page", (), {
        "PermissionPolicy": SimpleNamespace(PermissionGrantedByUser=True, PermissionDeniedByUser=False),
        "Feature": feature,
        "setFeaturePermission": lambda self, url, requested, allowed: decisions.append(allowed),
    })
    monkeypatch.setitem(sys.modules, "webview.platforms", SimpleNamespace(
        qt=SimpleNamespace(BrowserView=SimpleNamespace(WebPage=page))
    ))
    dashboard._patch_qt_permission_policy()
    for host in ("127.0.0.1", "example.com"):
        url = SimpleNamespace(scheme=lambda: "http", host=lambda: host)
        page().onFeaturePermissionRequested(url, feature.MediaAudioCapture)
    assert decisions == [True, False]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 自启注册表")
def test_autostart_preserves_arguments_and_failed_settings(monkeypatch):
    import winreg
    from notmyfault.host import tray

    values = {}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(winreg, "OpenKey", lambda *args: Key())
    monkeypatch.setattr(winreg, "SetValueEx", lambda key, name, reserved, kind, value: values.update({name: value}))
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: (values.get(name, ""), winreg.REG_SZ))
    monkeypatch.setattr(tray, "PROJECT_ROOT", r"C:\App Folder")
    monkeypatch.setattr(sys, "executable", r"C:\Python Folder\python.exe")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    icon = tray.TrayIcon()
    assert icon.set_auto_start(True) is True
    assert values[tray.AUTO_START_NAME] == subprocess.list2cmdline([
        sys.executable, r"C:\App Folder\NOTMYFAULT.pyw"
    ])
    assert tray._is_auto_start_enabled() is True
    monkeypatch.setattr(sys, "frozen", True)
    assert tray._register_auto_start() is True
    assert values[tray.AUTO_START_NAME] == subprocess.list2cmdline([sys.executable])
    monkeypatch.setattr(winreg, "DeleteValue", lambda *args: (_ for _ in ()).throw(PermissionError("denied")))
    assert icon.set_auto_start(False) is False
    assert icon._auto_start_enabled is True


def test_package_entry_constructs_signed_store(tmp_path, monkeypatch):
    from notmyfault.host import app

    paths = ApplicationPaths(tmp_path / "config", tmp_path / "package", tmp_path)
    stores = []
    monkeypatch.setattr(ApplicationPaths, "default", lambda: paths)
    monkeypatch.setattr(app, "run", lambda *, store: stores.append(store))
    runpy.run_module("notmyfault.__main__", run_name="__main__")
    assert stores[0].paths == paths
    assert not paths.config_dir.exists()
