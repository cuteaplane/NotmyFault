from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notmyfault.host.api.auth import ApiTokenStore
from notmyfault.host.api.middleware import install_api_middleware
from notmyfault.tests.api_support import API_TOKEN, make_api_env


EXPECTED_OPERATIONS = {
    ("POST", "/api/engine/start"),
    ("POST", "/api/engine/stop"),
    ("POST", "/api/engine/shutdown"),
    ("GET", "/api/engine/status"),
    ("GET", "/api/platform"),
    ("GET", "/api/engine/diagnostics"),
    ("GET", "/api/runs"),
    ("GET", "/api/runs/{run_id}"),
    ("POST", "/api/runs/{run_id}/cancel"),
    ("GET", "/api/engine/logs"),
    ("GET", "/api/engine/logs/files"),
    ("GET", "/api/engine/logs/entries"),
    ("GET", "/api/events"),
    ("GET", "/api/plugins/extensions"),
    ("POST", "/api/plugins/{plugin_id}/extensions/commands/{command_id}/invoke"),
    ("DELETE", "/api/plugins/{plugin_id}/extensions/sessions/{session_id}"),
    ("GET", "/api/plugins/{plugin_id}/extensions/views/{view_id}/page"),
    ("GET", "/api/config/security-status"),
    ("POST", "/api/config/security-approve"),
    ("GET", "/api/plugins"),
    ("GET", "/api/plugins/list"),
    ("POST", "/api/plugins/registry"),
    ("POST", "/api/plugins/registry/download"),
    ("POST", "/api/plugins/toggle"),
    ("POST", "/api/plugins/preview"),
    ("POST", "/api/plugins/install"),
    ("GET", "/api/plugins/key-status"),
    ("DELETE", "/api/plugins/{ptype}/{pid}"),
    ("GET", "/api/rules"),
    ("PUT", "/api/rules"),
    ("POST", "/api/rules/validate"),
    ("POST", "/api/rules/approve"),
    ("POST", "/api/rules/{rule_index}/run"),
    ("GET", "/api/settings/ai-drafting"),
    ("PUT", "/api/settings/ai-drafting"),
    ("PUT", "/api/settings/ai-drafting/api-key"),
    ("DELETE", "/api/settings/ai-drafting/api-key"),
    ("POST", "/api/rules/draft/ai"),
    ("POST", "/api/rules/draft/ai/stream"),
}


def concrete_path(path: str) -> str:
    return (
        path.replace("{run_id}", "missing")
        .replace("{plugin_id}", "missing")
        .replace("{command_id}", "missing")
        .replace("{session_id}", "missing")
        .replace("{view_id}", "missing")
        .replace("{ptype}", "actions")
        .replace("{pid}", "missing")
        .replace("{rule_index}", "0")
    )


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_routes_are_disabled(tmp_path, path):
    env = make_api_env(tmp_path)
    assert env.client.get(path).status_code == 404


def test_openapi_keeps_every_http_operation(tmp_path):
    env = make_api_env(tmp_path)
    actual = {
        (method.upper(), path)
        for path, operations in env.server.app.openapi()["paths"].items()
        for method in operations
    }
    assert actual == EXPECTED_OPERATIONS


@pytest.mark.parametrize("method,path", sorted(EXPECTED_OPERATIONS))
def test_every_api_operation_requires_authentication(tmp_path, method, path):
    env = make_api_env(tmp_path)
    response = env.client.request(method, concrete_path(path))
    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden: invalid API Token"}


def test_security_approval_returns_parse_reason(tmp_path):
    env = make_api_env(tmp_path)
    env.paths.config_file.write_text("{broken", encoding="utf-8")

    response = env.client.post(
        "/api/config/security-approve",
        headers=env.headers,
    )

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"].startswith("配置文件无法解析:")


@pytest.mark.parametrize("mode_name", ["strict", "normal", "permissive"])
def test_security_status_separates_installation_from_config_without_engine(
    tmp_path, monkeypatch, mode_name
):
    from notmyfault.host.api.services import settings
    from notmyfault.security.security import SecurityMode

    package_root = tmp_path / "notmyfault"
    plugin_dir = package_root / "actions" / "notify"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "action.json").write_text('{"id":"notify"}', encoding="utf-8")
    env = make_api_env(tmp_path, package_root=package_root)
    monkeypatch.setattr(settings, "detect_security_mode", lambda: SecurityMode(mode_name))
    monkeypatch.setattr(settings, "verify_file", lambda path: True)
    monkeypatch.setattr(settings, "verify_core_integrity", lambda: (True, []))

    body = env.client.get("/api/config/security-status", headers=env.headers).json()
    assert body["status"] == "ok"
    assert body["security_mode"] == mode_name
    assert body["installation"]["status"] == "invalid"
    assert body["installation"]["issues"] == [
        {"path": "actions/notify", "reason": "内置插件签名缺失或无效"}
    ]

    monkeypatch.setattr(settings, "inspect_signature", lambda *args: SimpleNamespace(kind="official"))
    env.paths.config_file.write_text("{broken", encoding="utf-8")
    body = env.client.get("/api/config/security-status", headers=env.headers).json()
    assert body["installation"]["status"] == "ok"
    assert body["status"] == "unreadable"


def test_options_preflight_is_allowed_for_dashboard_port(tmp_path):
    env = make_api_env(tmp_path)
    response = env.client.options(
        "/api/engine/status",
        headers={
            "Origin": "http://127.0.0.1:19218",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:19218"


def test_wrong_token_does_not_write_the_shared_token_file(tmp_path):
    env = make_api_env(tmp_path)
    env.paths.api_token_file.write_text("b" * 64, encoding="utf-8")
    response = env.client.get(
        "/api/engine/status",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 403
    assert env.paths.api_token_file.read_text(encoding="utf-8") == "b" * 64

    valid = env.client.get("/api/engine/status", headers=env.headers)
    assert valid.status_code == 200
    assert env.paths.api_token_file.read_text(encoding="utf-8") == API_TOKEN


def test_token_store_creates_token_and_reapplies_file_permissions(tmp_path):
    path = tmp_path / ".api_token"
    restricted = []
    store = ApiTokenStore(
        path,
        permission_restrictor=lambda value: restricted.append(Path(value)),
    )

    assert len(store.token) == 64
    assert int(store.token, 16) >= 0
    assert path.read_text(encoding="utf-8") == store.token
    assert len(restricted) == 1

    restricted.clear()
    reloaded = ApiTokenStore(
        path,
        permission_restrictor=lambda value: restricted.append(Path(value)),
    )
    assert reloaded.token == store.token
    assert len(restricted) == 1
    assert not reloaded.matches("无效令牌")
    path.write_text("损坏令牌", encoding="utf-8")
    reloaded.repair_file()
    assert path.read_text(encoding="utf-8") == store.token


def test_sse_rejects_query_token(tmp_path):
    token_path = tmp_path / ".api_token"
    token_store = ApiTokenStore(
        token_path,
        token=API_TOKEN,
        writer=lambda token: token_path.write_text(token, encoding="utf-8"),
    )
    app = FastAPI()
    install_api_middleware(app, token_store)

    @app.get("/api/events")
    async def events():
        return {"ok": True}

    response = TestClient(app).get(f"/api/events?token={API_TOKEN}")
    assert response.status_code == 403


def test_registry_download_contract(tmp_path):
    content = b"nmfp-content"
    sha256 = "b" * 64

    class Registry:
        def load(self, url):
            return {"schema_version": 1, "plugins": []}

        def download(self, url, package_name, version):
            return content, {"sha256": sha256}

    env = make_api_env(tmp_path, plugin_registry=Registry())
    response = env.client.post(
        "/api/plugins/registry/download",
        headers=env.headers,
        json={
            "url": "https://example.com/registry.json",
            "package_name": "com.example.demo",
            "version": "1.0.0",
        },
    )

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-plugin-sha256"] == sha256
    assert response.headers["content-disposition"] == (
        'attachment; filename="com.example.demo-1.0.0.nmfp"'
    )


def test_rule_save_rejects_missing_binding_source(tmp_path):
    env = make_api_env(tmp_path)
    response = env.client.put(
        "/api/rules",
        headers=env.headers,
        json={
            "rules": [
                {
                    "name": "无效引用",
                    "event": {
                        "type": "hotkey",
                        "binding_id": "t_hot001",
                        "params": {"hotkey": "ctrl+k"},
                    },
                    "actions": [
                        {
                            "type": "notify",
                            "binding_id": "a_note001",
                            "params": {
                                "message": {
                                    "$ref": {
                                        "scope": "step",
                                        "node": "a_missing001",
                                        "path": ["value"],
                                    }
                                }
                            },
                        }
                    ],
                }
            ]
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "规则数据绑定无效"
    assert response.json()["details"]

    from notmyfault.host.api.services.rules import RuleService, RuleServiceError
    service = RuleService(env.store, lambda: {
        "triggers": {"hotkey": {"params": []}},
        "actions": {"notify": {"params": [{"name": "message", "type": "string", "required": True}]}},
    })
    rule = {"name": "必填参数", "event": {"type": "hotkey", "params": {}},
            "actions": [{"type": "notify", "params": {}}]}
    assert service.validate_draft(rule)["valid"] is False
    with pytest.raises(RuleServiceError) as caught:
        service.save([rule], None)
    assert caught.value.status_code == 400
    assert env.store.load_verified_rules() == []


def test_rules_transport_preserves_typed_constants_and_literal_objects(tmp_path):
    from decimal import Decimal
    from notmyfault.core.value_codec import encode_value, decode_value

    env = make_api_env(tmp_path)
    values = {
        "count": 9007199254740993, "amount": Decimal("0.1234567890123456789"),
        "bytes": b"\x00\xff", "object": {"$nmf_value": {"type": "int", "data": "7"}},
        "expression": {"$ref": {"scope": "step", "node": "a_literal", "path": ["x"]}},
    }
    rule = {"name": "保存精确数据", "event": {"type": "hotkey", "params": {"hotkey": "ctrl+k"}},
            "constants": [{"id": "c_values01", "name": "数据", "value_type": "object", "value": {"$literal": values}}],
            "variables": [{"id": "v_result01", "name": "结果", "value_type": "object"}],
            "actions": [{"type": "set_variable", "variable": "v_result01", "value": {"$ref": {"scope": "constant", "node": "c_values01", "path": []}}}]}
    constraint = {"type": "int", "min": values["count"], "max": values["count"], "enum": [values["count"]]}
    rule["constants"].append({"id": "c_limits01", "name": "精确约束", "value_type": constraint, "value": values["count"]})
    response = env.client.put("/api/rules", headers={**env.headers, "X-NMF-Value-Encoding": "typed-v1"}, json=encode_value({"rules": [rule]}))
    assert response.status_code == 200, response.text
    stored = env.store.load_verified_rules()[0]
    assert stored["constants"][0]["value"]["$literal"] == values
    assert stored["constants"][1]["value_type"] == constraint
    assert stored["constants"][1]["value"] == values["count"]
    fetched = env.client.get("/api/rules", headers=env.headers)
    assert decode_value(fetched.json())["rules"][0] == stored
    saved_again = env.client.put("/api/rules", headers={**env.headers, "X-NMF-Value-Encoding": "typed-v1"}, json=fetched.json())
    assert saved_again.status_code == 200, saved_again.text
    assert env.store.load_verified_rules()[0] == stored
    updated = decode_value(fetched.json())
    updated["expected_revision"] = updated["revision"]
    updated["rules"][0]["name"] = "新的规则名称"
    accepted = env.client.put("/api/rules", headers={**env.headers, "X-NMF-Value-Encoding": "typed-v1"}, json=encode_value(updated))
    assert accepted.status_code == 200
    assert accepted.json()["revision"] != updated["expected_revision"]
    stale = env.client.put("/api/rules", headers={**env.headers, "X-NMF-Value-Encoding": "typed-v1"}, json=encode_value(updated))
    assert stale.status_code == 409
    assert stale.json()["code"] == "rules_conflict"


def test_engine_control_and_status_contract(tmp_path):
    env = make_api_env(tmp_path)
    started = env.client.post("/api/engine/start", headers=env.headers)
    assert started.status_code == 200
    assert started.json() == {
        "ok": True,
        "running": True,
        "engine_running": True,
        "engine_state": "running",
        "api_alive": True,
    }
    status = env.client.get("/api/engine/status", headers=env.headers)
    assert status.status_code == 200
    assert status.json()["running"] is True
    stopped = env.client.post("/api/engine/stop", headers=env.headers)
    assert stopped.status_code == 200
    assert stopped.json() == {
        "ok": True,
        "stopped": True,
        "stopping": False,
        "engine_running": False,
        "engine_state": "stopped",
        "api_alive": True,
    }


def test_application_paths_keep_existing_physical_names(tmp_path):
    env = make_api_env(tmp_path)
    assert env.paths.config_file == env.paths.config_dir / "config.json"
    assert env.paths.rules_file == env.paths.config_dir / "rules.json"
    assert env.paths.config_secret_file == env.paths.config_dir / ".config_secret"
    assert env.paths.api_token_file == env.paths.config_dir / ".api_token"
    assert env.paths.user_plugins_dir == env.paths.config_dir / "plugins"
    assert env.paths.private_dir == Path(tmp_path) / ".private"


def test_api_server_stop_ends_event_streams(tmp_path):
    async def exercise():
        env = make_api_env(tmp_path)
        subscription = env.event_broker.subscribe()

        env.server.stop()

        assert await asyncio.wait_for(subscription.queue.get(), timeout=1) is None

    asyncio.run(exercise())


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _open_event_stream(port: int, token: str) -> socket.socket:
    deadline = time.monotonic() + 10
    while True:
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=5)
            break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    request = (
        "GET /api/events HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: keep-alive\r\n\r\n"
    )
    connection.sendall(request.encode("ascii"))
    connection.settimeout(10)
    received = b""
    while b"\r\n\r\n" not in received:
        chunk = connection.recv(4096)
        if not chunk:
            connection.close()
            raise AssertionError("事件流还没建立就断开了")
        received += chunk
    assert b" 200 " in received.split(b"\r\n", 1)[0], received[:120]
    return connection


def _read_until(connection: socket.socket, marker: bytes, timeout: float = 10.0) -> bytes:
    connection.settimeout(timeout)
    received = b""
    while marker not in received:
        chunk = connection.recv(4096)
        if not chunk:
            break
        received += chunk
    return received


def test_api_server_shutdown_with_open_event_stream(tmp_path, monkeypatch):
    """Dashboard 挂着事件流时点退出，服务要在几秒内收尾，不能靠强杀进程"""
    flushes = []
    env = make_api_env(tmp_path)
    monkeypatch.setattr(
        env.run_history, "flush", lambda timeout=5.0: flushes.append(timeout) or True
    )
    port = _free_port()
    thread = threading.Thread(
        target=env.server.serve,
        kwargs={"host": "127.0.0.1", "port": port},
        daemon=True,
    )
    thread.start()
    connection = _open_event_stream(port, env.token_store.token)
    try:
        env.event_broker.publish("engine_state_changed", {"state": "running"})
        assert b"engine_state_changed" in _read_until(connection, b"engine_state_changed")

        started_at = time.monotonic()
        env.server.stop()
        thread.join(timeout=10)
        elapsed = time.monotonic() - started_at

        assert not thread.is_alive(), "事件流没结束，uvicorn 一直在等这条连接"
        assert elapsed < 5, f"关闭用了 {elapsed:.1f}s，说明事件流没有主动结束"
        assert flushes, "服务退出时没跑到运行历史落盘"
    finally:
        connection.close()
