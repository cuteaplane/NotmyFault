from __future__ import annotations

import asyncio
import runpy
import shutil
import signal
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

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
        package_root / "triggers" / "time_schedule", "trigger.json", signing_key
    )
    signing.sign_plugin(package_root / "actions" / "notify", "action.json", signing_key)
    paths = make_paths(tmp_path, package_root=package_root)
    (tmp_path / "build.json").write_text("{}", encoding="utf-8")
    (tmp_path / "build.json.sig").write_bytes(b"test-signature")
    store = make_store(paths)
    assert store.save_rules(
        [
            {
                "name": "装配初始规则",
                "event": {
                    "type": "time_schedule",
                    "params": {"time": "23:58"},
                },
                "actions": [
                    {
                        "type": "notify",
                        "params": {
                            "title": "NotmyFault",
                            "message": "assembly start",
                        },
                    }
                ],
            }
        ]
    )
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
        time.sleep(1.05)

        response = client.put(
            "/api/rules",
            headers=headers,
            json={
                "rules": [
                    {
                        "name": "装配热重载",
                        "event": {
                            "type": "time_schedule",
                            "params": {"time": "23:59"},
                        },
                        "actions": [
                            {
                                "type": "notify",
                                "params": {
                                    "title": "NotmyFault",
                                    "message": "assembly smoke",
                                },
                            }
                        ],
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
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
