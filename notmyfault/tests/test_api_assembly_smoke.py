from __future__ import annotations

import asyncio
import os
import runpy
import shutil
import signal
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from notmyfault.tests.canvas_fixtures import trigger_rule, set_rule_steps
from notmyfault.core.run_history import RunHistory
from notmyfault.host.api.auth import ApiTokenStore
from notmyfault.host.api.events import EventBroker
from notmyfault.host.api.plugin_installation import (
    PendingPreviewStore,
    PluginFileSystem,
    PluginTemporaryStorage,
)
from notmyfault.host.api_server import create_api_server
from notmyfault.host.app import create_engine
from notmyfault.host.plugin_registry import PluginRegistryClient
from notmyfault.security import signing, signing_keys
from notmyfault.tests.api_support import (
    API_TOKEN,
    FakeKeyStore,
    make_paths,
    make_store,
)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_real_api_engine_store_event_and_hot_reload_assembly(monkeypatch, tmp_path):
    monkeypatch.setattr("notmyfault.core.engine.AutomationEngine._alert_user", lambda *args, **kwargs: None)
    monkeypatch.setattr("notmyfault.platform.platform_support.show_notification", lambda *args, **kwargs: None)
    if sys.platform == "win32":
        monkeypatch.setattr("Win_toaster.AUMID_Register.register_toaster", lambda *args, **kwargs: None)
    launcher_path = Path(__file__).resolve().parents[2] / "NOTMYFAULT.pyw"
    namespace = runpy.run_path(str(launcher_path), run_name="notmyfault_assembly_test")
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        "notmyfault.core.engine.verify_core_integrity", lambda: (True, [])
    )
    source_package = Path(__file__).resolve().parents[1]
    package_root = tmp_path / "package"
    shutil.copytree(
        source_package / "triggers" / "time_schedule",
        package_root / "triggers" / "time_schedule",
    )
    shutil.copytree(
        source_package / "actions" / "notify",
        package_root / "actions" / "notify",
    )
    signing_key = Ed25519PrivateKey.generate()
    public_key = signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    monkeypatch.setattr(signing_keys, "get_public_keys", lambda: [public_key])
    signing.sign_plugin(
        package_root / "triggers" / "time_schedule", private_key=signing_key
    )
    signing.sign_plugin(package_root / "actions" / "notify", private_key=signing_key)
    paths = make_paths(tmp_path, package_root=package_root)
    (tmp_path / "build.json").write_text("{}", encoding="utf-8")
    (tmp_path / "build.json.sig").write_bytes(b"test-signature")
    store = make_store(paths)
    assert store.save_rules([set_rule_steps(
        trigger_rule("r_initial0001", "装配初始规则", "time_schedule", {"time": "23:58"}),
        [{"type": "notify", "params": {"title": "NotmyFault", "message": "assembly start"}}],
    )])
    runner = namespace["EngineRunner"](paths, store, create_engine)
    history = RunHistory(str(paths.run_history_file))
    events = EventBroker(history)
    token_store = ApiTokenStore(
        paths.api_token_file,
        token=API_TOKEN,
        writer=lambda token: paths.api_token_file.write_text(token, encoding="utf-8"),
    )
    server = create_api_server(
        runner,
        store,
        paths,
        token_store=token_store,
        ai_key_store=FakeKeyStore(),
        plugin_file_system=PluginFileSystem(),
        pending_previews=PendingPreviewStore(),
        plugin_temporary_storage=PluginTemporaryStorage(),
        plugin_registry=PluginRegistryClient(),
        run_history=history,
        event_broker=events,
    )
    runner._api = server
    runner._runtime.set_event_sink(server.publish_event)
    client = TestClient(server.app)
    headers = {"Authorization": f"Bearer {API_TOKEN}"}

    async def receive_state_event():
        subscription = events.subscribe()
        try:
            assert runner.start_engine() is True
            while True:
                packet = await asyncio.wait_for(subscription.queue.get(), timeout=15)
                assert packet["type"] != "engine_failed", packet["data"]
                if packet["type"] == "engine_state_changed" and packet["data"] == {
                    "state": "running"
                }:
                    return
        finally:
            events.unsubscribe(subscription)

    try:
        asyncio.run(receive_state_event())
        assert wait_for(lambda: runner.current_engine is not None)
        assert client.get("/api/engine/status", headers=headers).json()[
            "engine_state"
        ] == "running"
        assert wait_for(lambda: runner.current_engine._rule_watcher._thread is not None)
        previous_mtime = runner.current_engine._rule_watcher._stamp.mtime_ns / 1e9

        response = client.put(
            "/api/rules",
            headers=headers,
            json={"rules": [set_rule_steps(
                trigger_rule("r_reload0001", "装配热重载", "time_schedule", {"time": "23:59"}),
                [{"type": "notify", "params": {"title": "NotmyFault", "message": "assembly smoke"}}],
            )]},
        )
        assert response.status_code == 200, response.text
        os.utime(paths.rules_file, (previous_mtime + 2, previous_mtime + 2))
        assert wait_for(
            lambda: runner.current_engine is not None
            and runner.current_engine.rules[0]["name"] == "装配热重载",
            timeout=4,
        )

        stop = client.post("/api/engine/stop", headers=headers)
        assert stop.status_code == 200
        assert stop.json()["ok"] is True
        assert wait_for(lambda: runner.engine_state == "stopped", timeout=15)
    finally:
        runner.stop_engine()


def test_http_maps_binding_failure_without_changing_history_envelope():
    from notmyfault.core.event_stream import EventStream

    class History:
        def __init__(self):
            self.events = []

        def record_async(self, event):
            self.events.append(event)

    async def receive():
        history = History()
        broker = EventBroker(history)
        subscription = broker.subscribe()
        stream = EventStream()
        event = stream.publish("run.failed", {"run_id": "run_binding", "failure_kind": "binding", "error": {"code": "missing_binding_value", "message": "缺少运行数据"}})
        broker.publish(event)
        packet = await asyncio.wait_for(subscription.queue.get(), 1)
        assert packet["type"] == "workflow_failed"
        assert packet["data"]["error"]["code"] == "missing_binding_value"
        assert history.events[0]["type"] == "run.failed"
        assert history.events[0]["v"] == 1
        broker.close()
        stream.close()

    asyncio.run(receive())
