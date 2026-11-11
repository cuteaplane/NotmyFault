from __future__ import annotations

import asyncio
import socket
import threading
from types import SimpleNamespace

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


class WaitingProvider:
    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def stream(self, _messages, _schema, should_stop=None):
        self.started.set()
        self.cancelled.wait(3)
        if should_stop is not None and should_stop():
            return
        yield "result", {"result_type": "assistant_message", "message": "done"}

    def cancel(self):
        self.cancelled.set()


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


def test_stream_disconnect_stops_without_done(tmp_path, monkeypatch):
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

    reading = threading.Event()
    released = threading.Event()
    finished = threading.Event()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            finished.set()

        def read(self, _count):
            reading.set()
            assert released.wait(2)
            return b""

        def close(self):
            released.set()

    monkeypatch.setattr(ai_provider, "resolve_public_http_url", lambda *_args, **_kwargs: (None, ("8.8.8.8",)))
    monkeypatch.setattr(ai_provider, "_is_private_host", lambda _host: False)
    monkeypatch.setattr(ai_provider, "_build_request_opener", lambda _addresses, **_kwargs: SimpleNamespace(open=lambda *_args, **_kwargs: Response()))
    provider = OpenAICompatibleDraftProvider("https://example.invalid/v1", "test-model", "test-key")
    service, _store = build_service(tmp_path / "blocking", provider)
    plan = service.prepare_stream({"messages": [{"role": "user", "content": "提醒"}]})

    async def collect_blocking():
        return [item async for item in service.stream(plan, lambda: asyncio.sleep(0, reading.is_set()))]

    assert asyncio.run(collect_blocking()) == [("status", {"status": "started"})]
    assert released.is_set() and finished.is_set()


def test_service_shutdown_cancels_active_ai_stream(tmp_path):
    provider = WaitingProvider()
    service, _store = build_service(tmp_path, provider)
    plan = service.prepare_stream({"messages": [{"role": "user", "content": "提醒"}]})

    async def collect():
        stream = service.stream(plan, lambda: asyncio.sleep(0, False))
        first = await anext(stream)
        assert first == ("status", {"status": "started"})
        assert await asyncio.to_thread(provider.started.wait, 2)
        service.begin_shutdown()
        return [item async for item in stream]

    events = asyncio.run(collect())
    assert provider.cancelled.is_set()
    assert all(kind != "result" for kind, _payload in events)


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
    monkeypatch.setattr(ai_provider, "resolve_public_http_url", lambda url, **kwargs: (None, ("203.0.113.10",)))
    monkeypatch.setattr(ai_provider, "_build_request_opener", lambda addresses, **_kwargs: TimeoutOpener())
    provider = OpenAICompatibleDraftProvider("https://example.test/v1", "test-model", "test-key")
    monkeypatch.setattr(ai_provider, "_is_private_host", lambda _host: False)

    with pytest.raises(AIProviderIdleTimeoutError):
        next(
            provider.stream(
                [{"role": "user", "content": "提醒"}],
                {"triggers": {}, "actions": {}},
                idle_timeout=1,
            )
        )


def test_provider_cancel_interrupts_waiting_for_response_headers(monkeypatch):
    client_socket, server_socket = socket.socketpair()
    request_received = threading.Event()
    peer_closed = threading.Event()
    failures = []

    class PlaintextTLSContext:
        check_hostname = False

        def wrap_socket(self, sock, server_hostname, do_handshake_on_connect=True):
            class PlaintextSocket:
                def do_handshake(self):
                    return None

                def __getattr__(self, name):
                    return getattr(sock, name)

            return PlaintextSocket()

    def read_until_closed():
        try:
            assert server_socket.recv(4096)
            request_received.set()
            try:
                while server_socket.recv(4096):
                    pass
            except OSError:
                pass
        finally:
            peer_closed.set()

    def consume_headers(provider):
        try:
            next(provider.stream(
                [{"role": "user", "content": "提醒"}],
                {"triggers": {}, "actions": {}},
                idle_timeout=30,
            ))
        except Exception as error:
            failures.append(error)

    monkeypatch.setattr(ai_provider, "resolve_public_http_url", lambda *_args, **_kwargs: (None, ("203.0.113.10",)))
    monkeypatch.setattr(ai_provider, "_is_private_host", lambda _host: False)
    monkeypatch.setattr(ai_provider.ssl, "create_default_context", PlaintextTLSContext)
    monkeypatch.setattr(
        ai_provider.socket,
        "create_connection",
        lambda _address, _timeout, _source_address: client_socket,
    )
    provider = OpenAICompatibleDraftProvider(
        "https://example.test/v1", "test-model", "test-key"
    )
    server = threading.Thread(target=read_until_closed, daemon=True)
    consumer = threading.Thread(target=consume_headers, args=(provider,), daemon=True)
    server.start()
    consumer.start()
    try:
        assert request_received.wait(2)
        provider.cancel()
        assert peer_closed.wait(2)
        consumer.join(timeout=2)
        assert not consumer.is_alive()
        assert failures
    finally:
        provider.cancel()
        client_socket.close()
        server_socket.close()


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


def test_settings_never_write_api_key_to_config(tmp_path, monkeypatch):
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
    from notmyfault.host.api.services import ai_drafting
    from notmyfault.host.ai_provider import AIProviderRequestError

    key_store.save_api_key("saved-provider-key")
    service, _store = build_service(tmp_path / "provider", key_store=key_store)
    settings = enabled_config()["settings"]["ai_drafting"]
    monkeypatch.setattr(ai_drafting, "OpenAICompatibleDraftProvider", lambda **options: SimpleNamespace(**options))
    assert service._build_configured_provider({}, settings).api_key == "saved-provider-key"
    with pytest.raises(AIProviderRequestError):
        service._build_configured_provider({"endpoint_url": "https://other.invalid/v1"}, settings)
    temporary = service._build_configured_provider({
        "endpoint_url": "https://other.invalid/v1", "api_key": "temporary-key",
    }, settings)
    assert temporary.api_key == "temporary-key"
    changed = service.update_settings({**settings, "endpoint_url": "https://other.invalid/v1"})
    assert changed["settings"]["api_key_status"] == "none"
    with pytest.raises(AIProviderRequestError):
        service._build_configured_provider({}, changed["settings"])


def test_status_maps_injected_key_state(tmp_path):
    key_store = FakeKeyStore()
    key_store.status = KeyStoreStatus.UNSUPPORTED
    env = make_api_env(tmp_path, ai_key_store=key_store)
    response = env.client.get("/api/settings/ai-drafting", headers=env.headers)
    assert response.json()["api_key_status"] == "unsupported"


def test_sse_decoder_handles_split_frames_comments_and_done():
    decoder = ai_provider._SSEDecoder()
    first = decoder.feed(b'data: {"value": 1}\n\ndata: {"value":')
    second = decoder.feed(b' 2}\n\n: keepalive\n\ndata: [DONE]\n\n')
    assert first + second == [{"value": 1}, {"value": 2}, ai_provider._STREAM_DONE]

    crlf = ai_provider._SSEDecoder()
    assert crlf.feed(b'data: {"value": 3}\r\n\r\n') == [{"value": 3}]

    unfinished = ai_provider._SSEDecoder()
    assert unfinished.feed(b'data: {"value": 4}') == []
    assert unfinished.finish() == [{"value": 4}]


def test_chat_accumulator_defaults_missing_index_and_deduplicates_names():
    accumulator = ai_provider._ChatStreamAccumulator()
    accumulator.feed({"choices": [{"delta": {"tool_calls": [
        {"function": {"name": "draft_", "arguments": '{"a":'}}
    ]}}]})
    accumulator.feed({"choices": [{"delta": {"tool_calls": [
        {"function": {"name": "rule", "arguments": "1}"}}
    ]}}]})
    accumulator.feed({"choices": [{"delta": {"tool_calls": [
        {"function": {"name": "draft_rule", "arguments": ""}}
    ]}}]})
    call = accumulator.body()["choices"][0]["message"]["tool_calls"][0]["function"]
    assert call == {"name": "draft_rule", "arguments": '{"a":1}'}


class _FakeStreamOpener:
    def __init__(self, payload):
        self.payload = payload

    def open(self, _request, timeout):
        payload = self.payload

        class Response:
            def __init__(self):
                self.sent = False

            def read(self, _size):
                if self.sent:
                    return b""
                self.sent = True
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        return Response()


def test_stream_limit_counts_content_not_sse_framing(monkeypatch):
    import json

    event = {"choices": [{"delta": {"content": "x" * 40}}]}
    payload = f"data: {json.dumps(event)}\n\n".encode()
    monkeypatch.setattr(ai_provider, "resolve_public_http_url", lambda *_a, **_k: (None, ("203.0.113.10",)))
    monkeypatch.setattr(ai_provider, "_is_private_host", lambda _host: False)
    monkeypatch.setattr(ai_provider, "_MAX_STREAM_CONTENT_CHARS", 20)
    monkeypatch.setattr(
        ai_provider, "_build_request_opener",
        lambda *_a, **_k: _FakeStreamOpener(payload),
    )
    provider = OpenAICompatibleDraftProvider("https://example.test/v1", "test-model", "test-key")
    with pytest.raises(ai_provider.AIProviderRequestError, match="内容过大"):
        list(provider.stream(
            [{"role": "user", "content": "提醒"}],
            {"triggers": {}, "actions": {}},
            idle_timeout=1,
        ))
