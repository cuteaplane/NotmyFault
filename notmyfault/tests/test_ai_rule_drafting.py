from __future__ import annotations

import asyncio
import socket

import pytest

from notmyfault.host import ai_provider
from notmyfault.host.ai_provider import (
    AIProviderIdleTimeoutError,
    OpenAICompatibleDraftProvider,
)
from notmyfault.host.api.services.ai_drafting import (
    AIDraftingError,
    AIDraftingService,
)
from notmyfault.security.api_key_store import (
    KeyStoreError,
    KeyStoreStatus,
)
from notmyfault.tests.api_support import FakeKeyStore, make_api_env, make_paths, make_store


def enabled_config():
    return {
        "settings": {
            "ai_drafting": {
                "enabled": True,
                "endpoint_url": "https://example.invalid/v1",
                "model": "test-model",
                "api_format": "chat_completions",
            }
        }
    }


def candidate():
    return {
        "name": "AI 草稿",
        "event": {"type": "time_schedule", "params": {"time": "08:00"}},
        "actions": [{"type": "notify", "params": {"title": "早上好"}}],
    }


class RecordingProvider:
    def __init__(self, result=None, error=None):
        self.result = result or {"result_type": "rule_draft", "candidates": [candidate()]}
        self.error = error
        self.calls = []

    def __call__(self, messages, schema):
        self.calls.append((messages, schema))
        if self.error is not None:
            raise self.error
        return self.result


class StreamingProvider(RecordingProvider):
    def stream(self, messages, schema):
        self.calls.append((messages, schema))
        yield "reasoning", "分析"
        yield "text", "草稿"
        yield "result", self.result


class IdleTimeoutProvider:
    def __init__(self):
        self.idle_timeout = None

    def stream(
        self,
        messages,
        schema,
        should_stop=None,
        idle_timeout=None,
    ):
        self.idle_timeout = idle_timeout
        raise AIProviderIdleTimeoutError("idle")
        yield


class IdleTimeoutService(AIDraftingService):
    def __init__(self, *args, configured_provider, **kwargs):
        super().__init__(*args, **kwargs)
        self.configured_provider = configured_provider

    def _build_configured_provider(self, body, settings):
        return self.configured_provider


class FailingKeyStore(FakeKeyStore):
    def save_api_key(self, value: str) -> None:
        raise KeyStoreError("secret-value")


class TimeoutOpener:
    def open(self, _request, timeout):
        raise socket.timeout(f"timeout after {timeout}")


def build_service(tmp_path, provider=None, key_store=None):
    store = make_store(make_paths(tmp_path))
    assert store.save_config(enabled_config())
    schema = {
        "triggers": {"time_schedule": {}},
        "actions": {"notify": {}},
    }
    return AIDraftingService(
        store,
        lambda: schema,
        lambda rule: {"valid": True, "rule": rule},
        provider=provider,
        key_store=key_store or FakeKeyStore(),
    ), store


def test_sync_draft_validates_without_saving_rules(tmp_path):
    provider = RecordingProvider()
    service, store = build_service(tmp_path, provider)
    result = asyncio.run(
        service.draft({"messages": [{"role": "user", "content": "每天提醒"}]})
    )
    assert result["ok"] is True
    assert result["result_type"] == "rule_draft"
    assert result["validation"]["valid"] is True
    assert store.load_verified_rules() == []
    assert len(provider.calls) == 1


def test_provider_failure_is_generic_and_does_not_leak_secret(tmp_path):
    service, _store = build_service(
        tmp_path,
        RecordingProvider(error=RuntimeError("secret-value")),
    )
    with pytest.raises(AIDraftingError) as caught:
        asyncio.run(
            service.draft({"messages": [{"role": "user", "content": "提醒"}]})
        )
    assert caught.value.status_code == 502
    assert "secret-value" not in caught.value.body["error"]


def test_consent_field_is_rejected(tmp_path):
    service, _store = build_service(tmp_path, RecordingProvider())
    with pytest.raises(AIDraftingError) as caught:
        asyncio.run(service.draft({
            "messages": [{"role": "user", "content": "提醒"}],
            "consent": {"plugin_id": "example", "permissions": []},
        }))
    assert caught.value.status_code == 400
    assert caught.value.body["error"] == "consent 已停用"


def test_stream_emits_final_result_and_done(tmp_path):
    service, _store = build_service(tmp_path, StreamingProvider())
    plan = service.prepare_stream(
        {"messages": [{"role": "user", "content": "提醒"}]}
    )

    async def collect():
        return [item async for item in service.stream(plan, lambda: asyncio.sleep(0, False))]

    events = asyncio.run(collect())
    assert [kind for kind, _payload in events] == [
        "status",
        "reasoning",
        "text",
        "result",
        "done",
    ]


def test_stream_disconnect_stops_without_done(tmp_path):
    service, _store = build_service(tmp_path, StreamingProvider())
    plan = service.prepare_stream(
        {"messages": [{"role": "user", "content": "提醒"}]}
    )
    calls = 0

    async def disconnected():
        nonlocal calls
        calls += 1
        return calls > 1

    async def collect():
        return [item async for item in service.stream(plan, disconnected)]

    events = asyncio.run(collect())
    assert events[0] == ("status", {"status": "started"})
    assert all(kind != "done" for kind, _payload in events)


def test_stream_reports_120_second_idle_timeout(tmp_path):
    store = make_store(make_paths(tmp_path))
    assert store.save_config(enabled_config())
    provider = IdleTimeoutProvider()
    service = IdleTimeoutService(
        store,
        lambda: {"triggers": {}, "actions": {}},
        lambda rule: {"valid": True, "rule": rule},
        FakeKeyStore(),
        configured_provider=provider,
    )
    plan = service.prepare_stream(
        {"messages": [{"role": "user", "content": "提醒"}]}
    )

    async def collect():
        return [item async for item in service.stream(plan, lambda: asyncio.sleep(0, False))]

    events = asyncio.run(collect())
    assert provider.idle_timeout == 120
    assert events == [
        ("status", {"status": "started"}),
        (
            "error",
            {
                "code": "idle_timeout",
                "error": "AI 服务超过 120 秒没有返回内容，可能是模型响应太慢或网络问题",
            },
        ),
        ("done", {"status": "done"}),
    ]


def test_provider_stream_translates_socket_timeout(monkeypatch):
    provider = object.__new__(OpenAICompatibleDraftProvider)
    provider._model = "test-model"
    provider._api_format = "chat_completions"
    provider._api_key = "test-key"
    provider._request_host = "example.test"
    provider._request_url = "https://example.test/v1/chat/completions"
    provider._opener = TimeoutOpener()
    monkeypatch.setattr(ai_provider, "_is_private_host", lambda _host: False)

    with pytest.raises(AIProviderIdleTimeoutError):
        next(
            provider.stream(
                [{"role": "user", "content": "提醒"}],
                {"triggers": {}, "actions": {}},
                idle_timeout=1,
            )
        )


def test_api_key_endpoints_use_injected_store(tmp_path):
    key_store = FakeKeyStore()
    env = make_api_env(tmp_path, ai_key_store=key_store)
    saved = env.client.put(
        "/api/settings/ai-drafting/api-key",
        json={"api_key": "sk-fixed"},
        headers=env.headers,
    )
    assert saved.status_code == 200
    assert saved.json() == {"ok": True, "api_key_status": "saved"}
    assert key_store.value == "sk-fixed"
    deleted = env.client.delete(
        "/api/settings/ai-drafting/api-key", headers=env.headers
    )
    assert deleted.json() == {"ok": True, "api_key_status": "none"}


def test_key_store_failure_is_generic(tmp_path):
    env = make_api_env(tmp_path, ai_key_store=FailingKeyStore())
    response = env.client.put(
        "/api/settings/ai-drafting/api-key",
        json={"api_key": "secret-value"},
        headers=env.headers,
    )
    assert response.status_code == 500
    assert "secret-value" not in response.text


def test_settings_never_write_api_key_to_config(tmp_path):
    key_store = FakeKeyStore()
    env = make_api_env(tmp_path, ai_key_store=key_store)
    response = env.client.put(
        "/api/settings/ai-drafting",
        json={
            "enabled": True,
            "endpoint_url": "https://example.invalid/v1",
            "model": "m",
            "api_format": "responses",
            "api_key": "must-not-be-written",
        },
        headers=env.headers,
    )
    assert response.status_code == 200
    raw = env.paths.config_file.read_text(encoding="utf-8")
    assert "must-not-be-written" not in raw


def test_status_maps_injected_key_state(tmp_path):
    key_store = FakeKeyStore()
    key_store.status = KeyStoreStatus.UNSUPPORTED
    env = make_api_env(tmp_path, ai_key_store=key_store)
    response = env.client.get("/api/settings/ai-drafting", headers=env.headers)
    assert response.json()["api_key_status"] == "unsupported"
