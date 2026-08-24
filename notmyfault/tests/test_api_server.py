from __future__ import annotations

from pathlib import Path

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
    ("POST", "/api/desktop-elements/capture"),
    ("POST", "/api/desktop-elements/check"),
    ("GET", "/api/engine/logs"),
    ("GET", "/api/events"),
    ("GET", "/api/plugins/extensions"),
    ("POST", "/api/plugins/{plugin_id}/extensions/commands/{command_id}/invoke"),
    ("DELETE", "/api/plugins/{plugin_id}/extensions/sessions/{session_id}"),
    ("GET", "/api/plugins/{plugin_id}/extensions/views/{view_id}/page"),
    ("GET", "/api/plugins/components"),
    ("POST", "/api/plugins/{plugin_id}/components/{component_id}/invoke"),
    ("GET", "/api/config/security-status"),
    ("GET", "/api/settings/admin-authorization"),
    ("PUT", "/api/settings/admin-authorization"),
    ("GET", "/api/settings/admin-rule-verification"),
    ("PUT", "/api/settings/admin-rule-verification"),
    ("POST", "/api/config/security-approve"),
    ("GET", "/api/plugins"),
    ("GET", "/api/plugins/list"),
    ("POST", "/api/plugins/registry"),
    ("POST", "/api/plugins/registry/download"),
    ("GET", "/api/settings/bluetooth"),
    ("POST", "/api/settings/bluetooth/install"),
    ("POST", "/api/settings/bluetooth/uninstall"),
    ("POST", "/api/plugins/toggle"),
    ("POST", "/api/plugins/preview"),
    ("POST", "/api/plugins/install"),
    ("GET", "/api/plugins/key-status"),
    ("POST", "/api/plugins/install-source"),
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

AUTHENTICATED_CONTRACTS = [
    ("POST", "/api/engine/shutdown", None, 200, {"message", "ok"}),
    (
        "GET",
        "/api/engine/status",
        None,
        200,
        {
            "actions_count",
            "api_alive",
            "engine_running",
            "engine_state",
            "last_error",
            "pid",
            "rules_count",
            "running",
            "scheduler",
            "security_mode",
            "triggers_count",
        },
    ),
    (
        "GET",
        "/api/platform",
        None,
        200,
        {"capabilities", "desktop", "limitations", "platform", "session_type"},
    ),
    (
        "GET",
        "/api/engine/diagnostics",
        None,
        200,
        {"actions", "plugins", "rules", "uptime_seconds"},
    ),
    ("GET", "/api/runs", None, 200, {"runs"}),
    ("GET", "/api/runs/missing", None, 404, {"detail"}),
    ("POST", "/api/runs/missing/cancel", None, 404, {"error", "ok"}),
    (
        "POST",
        "/api/desktop-elements/capture",
        {},
        200,
        {"ok", "selector"},
    ),
    (
        "POST",
        "/api/desktop-elements/check",
        {"selector": {"x": 1}},
        200,
        {"matched", "ok"},
    ),
    ("GET", "/api/engine/logs", None, 200, {"lines", "total"}),
    (
        "GET",
        "/api/plugins/extensions",
        None,
        200,
        {"commands", "data_types", "parameter_editors", "views"},
    ),
    (
        "POST",
        "/api/plugins/missing/extensions/commands/missing/invoke",
        {},
        409,
        {"error", "ok"},
    ),
    (
        "DELETE",
        "/api/plugins/missing/extensions/sessions/missing",
        None,
        200,
        {"ok"},
    ),
    (
        "GET",
        "/api/plugins/missing/extensions/views/missing/page",
        None,
        409,
        {"error", "ok"},
    ),
    ("GET", "/api/plugins/components", None, 200, {"components"}),
    (
        "POST",
        "/api/plugins/missing/components/missing/invoke",
        {},
        404,
        {"error", "ok"},
    ),
    (
        "GET",
        "/api/config/security-status",
        None,
        200,
        {"reason", "status", "summary"},
    ),
    (
        "GET",
        "/api/settings/admin-authorization",
        None,
        200,
        {"effective_mode", "mode", "restart_required", "supported_modes"},
    ),
    (
        "PUT",
        "/api/settings/admin-authorization",
        {"mode": "per_execution"},
        200,
        {"effective_mode", "mode", "ok", "restart_required"},
    ),
    (
        "GET",
        "/api/settings/admin-rule-verification",
        None,
        200,
        {"key_verification"},
    ),
    (
        "PUT",
        "/api/settings/admin-rule-verification",
        {"key_verification": True},
        200,
        {"key_verification", "ok"},
    ),
    ("POST", "/api/config/security-approve", None, 200, {"message", "ok"}),
    ("GET", "/api/plugins", None, 200, {"actions", "triggers"}),
    ("GET", "/api/plugins/list", None, 200, {"actions", "triggers"}),
    ("POST", "/api/plugins/registry", {}, 400, {"error", "ok"}),
    (
        "POST",
        "/api/plugins/registry/download",
        {},
        400,
        {"error", "ok"},
    ),
    (
        "GET",
        "/api/settings/bluetooth",
        None,
        200,
        {"available", "installed", "meta"},
    ),
    (
        "POST",
        "/api/settings/bluetooth/install",
        None,
        200,
        {"ok", "restart_required"},
    ),
    (
        "POST",
        "/api/settings/bluetooth/uninstall",
        None,
        200,
        {"error", "ok"},
    ),
    ("POST", "/api/plugins/toggle", {}, 400, {"error", "ok"}),
    ("POST", "/api/plugins/preview", None, 400, {"error", "ok"}),
    ("POST", "/api/plugins/install", None, 400, {"error", "ok"}),
    ("GET", "/api/plugins/key-status", None, 200, {"encrypted", "exists"}),
    ("POST", "/api/plugins/install-source", {}, 400, {"error", "ok"}),
    (
        "DELETE",
        "/api/plugins/actions/missing",
        None,
        200,
        {"error", "ok"},
    ),
    ("GET", "/api/rules", None, 200, {"rules"}),
    ("PUT", "/api/rules", {"rules": []}, 200, {"ok", "rules"}),
    (
        "POST",
        "/api/rules/validate",
        {"rule": {}},
        200,
        {"issues", "ok", "summary", "valid"},
    ),
    ("POST", "/api/rules/approve", {"rules": []}, 200, {"ok"}),
    ("POST", "/api/rules/0/run", None, 409, {"error", "ok"}),
    (
        "GET",
        "/api/settings/ai-drafting",
        None,
        200,
        {"api_format", "api_key_status", "enabled", "endpoint_url", "model"},
    ),
    (
        "PUT",
        "/api/settings/ai-drafting",
        {},
        200,
        {"ok", "settings"},
    ),
    (
        "PUT",
        "/api/settings/ai-drafting/api-key",
        {},
        400,
        {"error", "ok"},
    ),
    (
        "DELETE",
        "/api/settings/ai-drafting/api-key",
        None,
        200,
        {"api_key_status", "ok"},
    ),
    ("POST", "/api/rules/draft/ai", {}, 403, {"code", "ok"}),
    ("POST", "/api/rules/draft/ai/stream", {}, 403, {"code", "ok"}),
]


def concrete_path(path: str) -> str:
    return (
        path.replace("{run_id}", "missing")
        .replace("{plugin_id}", "missing")
        .replace("{command_id}", "missing")
        .replace("{session_id}", "missing")
        .replace("{view_id}", "missing")
        .replace("{component_id}", "missing")
        .replace("{ptype}", "actions")
        .replace("{pid}", "missing")
        .replace("{rule_index}", "0")
    )


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


@pytest.mark.parametrize(
    "method,path,body,status_code,fields",
    AUTHENTICATED_CONTRACTS,
)
def test_authenticated_endpoint_contract(
    tmp_path,
    method,
    path,
    body,
    status_code,
    fields,
):
    env = make_api_env(tmp_path)
    kwargs = {"headers": env.headers}
    if body is not None:
        kwargs["json"] = body

    response = env.client.request(method, path, **kwargs)

    assert response.status_code == status_code
    assert set(response.json()) == fields


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


def test_wrong_token_repairs_the_shared_token_file(tmp_path):
    env = make_api_env(tmp_path)
    env.paths.api_token_file.write_text("b" * 64, encoding="utf-8")
    response = env.client.get(
        "/api/engine/status",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 403
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


def test_sse_accepts_query_token(tmp_path):
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
    assert response.status_code == 200
    assert response.json() == {"ok": True}


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


def test_engine_control_and_status_contract(tmp_path):
    env = make_api_env(tmp_path)
    started = env.client.post("/api/engine/start", headers=env.headers)
    assert started.status_code == 200
    assert started.json() == {
        "ok": True,
        "running": True,
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
