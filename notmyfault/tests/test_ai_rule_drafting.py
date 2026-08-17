"""AI 规则草稿和提前审批端点的契约测试。

AI 草稿只做检查，规则写入仍然只有 PUT /api/rules 一条路。
"""

import asyncio
import io
import json
import socket
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from urllib import request as urllib_request

import notmyfault.config as config_mod
from notmyfault.host import api_server
from notmyfault.host.api_server import EngineAPI
from notmyfault.security import api_key_store as store


class FakeRunner:
    def __init__(self):
        self.engine_running = False
        self.engine_state = "stopped"
        self.current_engine = None
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.start_result = True

    def start_engine(self):
        self.start_calls += 1
        if self.start_result:
            self.engine_running = True
            self.engine_state = "running"
        return self.start_result

    def stop_engine(self):
        self.stop_calls += 1
        self.engine_running = False
        self.engine_state = "stopped"
        return True

    def request_process_shutdown(self, force_after=10):
        self.shutdown_calls += 1


class FakeEngine:
    """记录手动执行调用，用于验证草稿流程不会真的跑规则。"""

    def __init__(self):
        self.calls = []

    def run_manual_rule_snapshot(self, rule, index, **kwargs):
        self.calls.append({"rule": rule, "index": index, "kwargs": kwargs})
        return True, "已执行", "run_fake001"


@pytest.fixture(autouse=True)
def stable_provider_test_dns(monkeypatch):
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host == "example.invalid":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ]
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr(api_server, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", config_file)
    rules_file = str(tmp_path / "rules.json")
    monkeypatch.setattr(api_server, "RULES_FILE", rules_file)
    monkeypatch.setattr(config_mod, "RULES_FILE", rules_file)
    monkeypatch.setattr(api_server, "API_TOKEN_FILE", str(tmp_path / ".api_token"))
    # 密钥存储作为接缝打桩：任何测试都不得触碰真实用户凭据。
    monkeypatch.setattr(
        api_server.api_key_store,
        "api_key_status",
        lambda: store.KeyStoreStatus.UNSUPPORTED,
    )
    monkeypatch.setattr(api_server.api_key_store, "load_api_key", lambda: None)
    monkeypatch.setattr(
        api_server.api_key_store,
        "delete_api_key",
        lambda: None,
    )

    def _unsupported_save(_key):
        raise store.KeyStoreUnsupportedError("测试未打桩的保存调用")

    monkeypatch.setattr(api_server.api_key_store, "save_api_key", _unsupported_save)
    runner = FakeRunner()
    api = EngineAPI(runner)
    api._get_user_plugins_dir = lambda: str(tmp_path / "user_plugins")
    client = TestClient(api.app)
    headers = {"Authorization": f"Bearer {api_server.API_TOKEN}"}
    return SimpleNamespace(
        api=api, runner=runner, client=client, headers=headers, tmp_path=tmp_path
    )


def ai_candidate():
    return {
        "name": "AI 草稿规则",
        "trigger": {"type": "usb_insert", "params": {}},
        "actions": [{"type": "open_url", "params": {"url": "http://example.com"}}],
    }


def ai_selector(
    name="提醒",
    trigger_type="time_schedule",
    trigger_params=None,
    action_type="notify",
    action_params=None,
):
    return {
        "name": name,
        "event": {"type": trigger_type, "params": trigger_params or {}},
        "actions": [{"type": action_type, "params": action_params or {}}],
    }


def plugin_proposal_args():
    return {
        "kind": "action",
        "id": "webhook_notify",
        "name": "Webhook 通知",
        "description": "把事件 POST 到用户给的地址",
    }


def single_user_message(text):
    """把一段用户文本包成单条多轮消息列表。"""
    return [{"role": "user", "content": text}]


def plugin_source_args(**overrides):
    """一份能通过 review_plugin_source 的动作源码与清单。"""
    args = {
        "kind": "action",
        "manifest": {
            "id": "notify_world",
            "name": "通知世界",
            "description": "发一条桌面通知",
            "enabled": True,
            "version_code": 1,
            "version": "1.0",
            "package_name": "io.github.notmyfault.notify_world",
        },
        "source": "def run(meta, params):\n    return {'ok': True}\n",
    }
    args.update(overrides)
    return args


def _request_tool_names(body):
    names = set()
    for definition in body.get("tools", []):
        if not isinstance(definition, dict):
            continue
        function = definition.get("function")
        name = function.get("name") if isinstance(function, dict) else definition.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def enabled_ai_config():
    return {
        "settings": {
            "ai_drafting": {
                "enabled": True,
                "endpoint_url": "https://example.invalid/v1",
                "model": "test-model",
            }
        }
    }


@pytest.fixture
def fake_store(monkeypatch):
    """内存假密钥存储：端点测试读写状态而不触碰真实用户凭据。"""
    state = {
        "saved": None,
        "status": store.KeyStoreStatus.ABSENT,
        "save_calls": [],
        "delete_calls": [],
        "load_calls": [],
    }

    def save(key):
        state["save_calls"].append(key)
        state["saved"] = key
        state["status"] = store.KeyStoreStatus.STORED

    def delete():
        state["delete_calls"].append(True)
        state["saved"] = None
        state["status"] = store.KeyStoreStatus.ABSENT

    def load():
        state["load_calls"].append(True)
        return state["saved"]

    def status():
        return state["status"]

    monkeypatch.setattr(api_server.api_key_store, "save_api_key", save)
    monkeypatch.setattr(api_server.api_key_store, "delete_api_key", delete)
    monkeypatch.setattr(api_server.api_key_store, "load_api_key", load)
    monkeypatch.setattr(api_server.api_key_store, "api_key_status", status)
    return state


class RecordingProvider:
    """假模型供应商。实现约定：EngineAPI 用实例属性 ai_draft_provider 调它，
    传多轮消息、插件 schema 和是否允许生成源码，供应商只回结构化结果。"""

    def __init__(self, candidates=None, error=None, proposal=None, result=None):
        self.calls = []
        self.candidates = candidates if candidates is not None else []
        self.proposal = proposal
        self.error = error
        self.result = result

    def __call__(self, messages, schema, allow_plugin_source=False):
        self.calls.append({
            "messages": messages,
            "schema": schema,
            "allow_plugin_source": allow_plugin_source,
        })
        if self.error is not None:
            raise RuntimeError(self.error)
        if self.result is not None:
            return self.result
        if self.proposal is not None:
            return {
                "ok": True,
                "result_type": "plugin_proposal",
                "proposal": self.proposal,
            }
        return {"ok": True, "candidates": self.candidates}


class StubOpener:
    """记录 open 的请求，直接回一个现成响应。"""

    def __init__(self, response):
        self.calls = []
        self.response = response

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        return self.response


class StubHttpResponse(io.BytesIO):
    """凑一个 HTTP 响应给 urllib 的 handler 链用，不碰真网络。"""

    def __init__(self, url, code, headers, raw=b""):
        super().__init__(raw)
        self.url = url
        self.code = code
        self.msg = "Stub response"
        self.headers = headers

    def info(self):
        return self.headers


class FakeJsonResponse:
    """把现成 payload 伪装成 opener 拿到的 HTTP 响应。"""

    def __init__(self, payload):
        self.payload = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.payload


class RedirectingStub(urllib_request.BaseHandler):
    """第一个请求回 301 指向 127.0.0.1，127.0.0.1 的请求记下来回 200。"""

    def __init__(self, contacted):
        self.contacted = contacted

    def http_open(self, req):
        if req.host == "127.0.0.1":
            self.contacted.append(req.full_url)
            return StubHttpResponse(req.full_url, 200, {})
        return StubHttpResponse(
            req.full_url,
            301,
            {"location": "http://127.0.0.1:8080/internal"},
        )

    https_open = http_open


class TestAiRuleDrafting:
    def test_provider_does_not_block_the_api_event_loop(self, api_env):
        class LoopProbeProvider:
            def __init__(self):
                self.ran_on_event_loop = None

            def __call__(self, messages, schema, allow_plugin_source=False):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    self.ran_on_event_loop = False
                else:
                    self.ran_on_event_loop = True
                return {
                    "candidates": [{
                        "name": "提醒",
                        "event": {"type": "time_schedule", "params": {}},
                        "actions": [{"type": "notify", "params": {}}],
                    }],
                }

        provider = LoopProbeProvider()
        api_env.api.ai_draft_provider = provider
        assert api_env.api._save_config(enabled_ai_config()) is True
        response = api_env.client.post(
            "/api/rules/draft/ai",
            headers=api_env.headers,
            json={"messages": single_user_message("每天九点提醒我")},
        )

        assert response.status_code == 200
        assert provider.ran_on_event_loop is False

    def test_provider_construction_does_not_block_the_api_event_loop(
        self, api_env, monkeypatch
    ):
        class ConstructorProbeProvider:
            ran_on_event_loop = None

            def __init__(self, **kwargs):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    type(self).ran_on_event_loop = False
                else:
                    type(self).ran_on_event_loop = True

            def __call__(self, messages, schema, *, allow_plugin_source=False):
                return {"candidates": [ai_candidate()]}

        monkeypatch.setattr(
            api_server, "OpenAICompatibleDraftProvider", ConstructorProbeProvider
        )
        assert api_env.api._save_config(enabled_ai_config()) is True

        response = api_env.client.post(
            "/api/rules/draft/ai",
            headers=api_env.headers,
            json={"messages": single_user_message("显示提醒"), "api_key": "session-key"},
        )

        assert response.status_code == 200
        assert ConstructorProbeProvider.ran_on_event_loop is False

    @pytest.mark.parametrize(
        ("api_format", "body"),
        [
            (
                "chat_completions",
                {
                    "choices": [{"message": {"content": (
                        "```json\n"
                        '{"name":"提醒","event":{"type":"time_schedule",'
                        '"params":{"time":"09:00"}},"actions":[{"type":"notify",'
                        '"params":{"message":"检查日报"}}]}\n'
                        "```"
                    )}}],
                },
            ),
            (
                "responses",
                {
                    "output": [{"type": "message", "content": [{
                        "type": "output_text",
                        "text": (
                            "草稿如下：\n```json\n"
                            '{"name":"提醒","event":{"type":"time_schedule",'
                            '"params":{"time":"09:00"}},"actions":[{"type":"notify",'
                            '"params":{"message":"检查日报"}}]}\n'
                            "```"
                        ),
                    }]}],
                },
            ),
        ],
    )
    def test_provider_free_text_falls_back_to_assistant_message(self, api_format, body):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format=api_format,
        )
        setattr(provider, "_opener", StubOpener(FakeJsonResponse(body)))

        result = provider(single_user_message("每天九点提醒我"), {
            "triggers": {"time_schedule": {"name": "定时"}},
            "actions": {"notify": {"name": "显示通知"}},
        })

        assert result["ok"] is True
        assert result["result_type"] == "assistant_message"
        assert "提醒" in result["message"]
        assert "time_schedule" in result["message"]

    @pytest.mark.parametrize(
        ("api_format", "body"),
        [
            ("chat_completions", {"choices": [{"message": {"content": ""}}]}),
            ("chat_completions", {"choices": [{"message": {"content": "   "}}]}),
            ("responses", {"output": []}),
            (
                "responses",
                {"output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "  "},
                ]}]},
            ),
        ],
        ids=["chat_empty", "chat_whitespace", "responses_empty", "responses_whitespace"],
    )
    def test_provider_blank_free_text_is_still_no_tool_call(self, api_format, body):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider
        from notmyfault.host.ai_tools import ToolCallError

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format=api_format,
        )
        setattr(provider, "_opener", StubOpener(FakeJsonResponse(body)))

        with pytest.raises(ToolCallError) as exc:
            provider(single_user_message("每天九点提醒我"), {
                "triggers": {"time_schedule": {"name": "定时"}},
                "actions": {"notify": {"name": "显示通知"}},
            })
        assert exc.value.code == "no_tool_call"

    def test_local_rule_draft_endpoint_is_not_available(self, api_env):
        response = api_env.client.post(
            "/api/rules/draft",
            json={"description": "每天九点提醒我"},
            headers=api_env.headers,
        )

        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("api_format", "response_payload", "expected_path", "expected_input_key"),
        [
            (
                "chat_completions",
                {
                    "choices": [{"message": {"tool_calls": [{
                        "function": {
                            "name": "propose_rule_draft",
                            "arguments": json.dumps(ai_selector(
                                name="打开网站",
                                trigger_type="usb_insert",
                                action_type="open_url",
                            )),
                        },
                    }]}}],
                },
                "/chat/completions",
                "messages",
            ),
            (
                "responses",
                {
                    "output": [{
                        "type": "function_call",
                        "name": "propose_rule_draft",
                        "arguments": json.dumps(ai_selector(
                            name="打开网站",
                            trigger_type="usb_insert",
                            action_type="open_url",
                        )),
                        "call_id": "call_format_2",
                    }],
                },
                "/responses",
                "input",
            ),
        ],
    )
    def test_openai_provider_supports_chat_and_responses_formats(
        self, api_format, response_payload, expected_path, expected_input_key
    ):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format=api_format,
        )
        opener = StubOpener(FakeJsonResponse(response_payload))
        setattr(provider, "_opener", opener)

        result = provider(single_user_message("插入 U 盘后打开网站"), {
            "triggers": {"usb_insert": {"name": "U 盘插入"}},
            "actions": {"open_url": {"name": "打开网址"}},
        })

        assert result["ok"] is True
        assert result["result_type"] == "rule_draft"
        assert result["candidates"][0]["event"]["type"] == "usb_insert"
        captured_request = opener.calls[0][0]
        request_body = json.loads(captured_request.data)
        assert captured_request.full_url.endswith(expected_path)
        assert expected_input_key in request_body
        assert request_body["tool_choice"] == "auto"
        assert _request_tool_names(request_body) == {
            "propose_rule_draft", "propose_plugin", "reply",
        }

    @pytest.mark.parametrize(
        ("api_format", "body"),
        [
            (
                "chat_completions",
                {"choices": [{"message": {"tool_calls": [{
                    "function": {
                        "name": "propose_plugin",
                        "arguments": json.dumps(plugin_proposal_args()),
                    },
                }]}}]},
            ),
            (
                "responses",
                {"output": [{
                    "type": "function_call",
                    "name": "propose_plugin",
                    "arguments": json.dumps(plugin_proposal_args()),
                    "call_id": "call_proposal_9",
                }]},
            ),
        ],
    )
    def test_provider_returns_plugin_proposal(self, api_format, body):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format=api_format,
        )
        setattr(provider, "_opener", StubOpener(FakeJsonResponse(body)))

        result = provider(single_user_message("需要一个能发 webhook 通知的动作"), {
            "triggers": {"time_schedule": {"name": "定时"}},
            "actions": {"notify": {"name": "显示通知"}},
        })

        assert result == {
            "ok": True,
            "result_type": "plugin_proposal",
            "proposal": plugin_proposal_args(),
        }

    @pytest.mark.parametrize(
        ("api_format", "body", "expected_code"),
        [
            (
                "chat_completions",
                {"choices": [{"message": {"tool_calls": [
                    {"function": {"name": "propose_rule_draft", "arguments": "{}"}},
                    {"function": {"name": "propose_plugin", "arguments": "{}"}},
                ]}}]},
                "multiple_tool_calls",
            ),
            (
                "chat_completions",
                {"choices": [{"message": {"tool_calls": [
                    {"function": {"name": "run_code", "arguments": "{}"}},
                ]}}]},
                "unknown_tool",
            ),
            (
                "chat_completions",
                {"choices": [{"message": {"tool_calls": [
                    {"function": {"name": "propose_rule_draft", "arguments": "not-json"}},
                ]}}]},
                "bad_arguments",
            ),
            (
                "responses",
                {"output": [
                    {"type": "function_call", "name": "propose_rule_draft",
                     "arguments": "{}", "call_id": "a"},
                    {"type": "function_call", "name": "propose_plugin",
                     "arguments": "{}", "call_id": "b"},
                ]},
                "multiple_tool_calls",
            ),
            (
                "responses",
                {"output": [
                    {"type": "function_call", "name": "delete_rules",
                     "arguments": "{}", "call_id": "c"},
                ]},
                "unknown_tool",
            ),
            (
                "responses",
                {"output": [
                    {"type": "function_call", "name": "propose_rule_draft",
                     "arguments": "{{{{", "call_id": "d"},
                ]},
                "bad_arguments",
            ),
        ],
    )
    def test_provider_rejects_bad_tool_calls(self, api_format, body, expected_code):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider
        from notmyfault.host.ai_tools import ToolCallError

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format=api_format,
        )
        setattr(provider, "_opener", StubOpener(FakeJsonResponse(body)))

        with pytest.raises(ToolCallError) as exc:
            provider(single_user_message("每天九点提醒我"), {
                "triggers": {"time_schedule": {"name": "定时"}},
                "actions": {"notify": {"name": "显示通知"}},
            })
        assert exc.value.code == expected_code

    def test_redirect_response_is_rejected_before_following(self):
        from urllib import error as urllib_error
        from notmyfault.host.ai_provider import _NoRedirects

        contacted = []
        opener = urllib_request.OpenerDirector()
        opener.add_handler(RedirectingStub(contacted))
        opener.add_handler(_NoRedirects())
        opener.add_handler(urllib_request.HTTPErrorProcessor())
        opener.add_handler(urllib_request.HTTPDefaultErrorHandler())

        with pytest.raises(urllib_error.HTTPError):
            opener.open(
                urllib_request.Request("http://example.invalid/start"), timeout=5
            )
        assert contacted == []

    def test_provider_redirect_fails_without_second_request(self, monkeypatch):
        from notmyfault.host import ai_provider
        from notmyfault.host.ai_provider import (
            OpenAICompatibleDraftProvider,
            _NoRedirects,
        )

        contacted = []

        def fake_build_opener():
            opener = urllib_request.OpenerDirector()
            opener.add_handler(RedirectingStub(contacted))
            opener.add_handler(_NoRedirects())
            opener.add_handler(urllib_request.HTTPErrorProcessor())
            opener.add_handler(urllib_request.HTTPDefaultErrorHandler())
            return opener

        monkeypatch.setattr(ai_provider, "_build_request_opener", fake_build_opener)
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        with pytest.raises(ValueError, match="AI 草稿请求失败"):
            provider(single_user_message("插入 U 盘后打开网站"), {"triggers": {}, "actions": {}})
        assert contacted == []

    def test_default_request_opener_rejects_redirects(self):
        from notmyfault.host.ai_provider import _build_request_opener, _NoRedirects

        opener = _build_request_opener()
        handlers = getattr(opener, "handlers", [])
        assert any(isinstance(handler, _NoRedirects) for handler in handlers)

    @pytest.mark.parametrize(
        "resolved_addresses",
        [
            ["127.0.0.1"],
            ["93.184.216.34", "10.0.0.8"],
        ],
        ids=["private_only", "mixed_public_private"],
    )
    def test_provider_rejects_hostname_resolving_to_private_address(
        self, monkeypatch, resolved_addresses
    ):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
                for address in resolved_addresses
            ],
        )

        with pytest.raises(ValueError, match="内网地址"):
            OpenAICompatibleDraftProvider(
                endpoint_url="https://provider.example/v1",
                model="draft-model",
                api_key="unit-test-secret",
            )

    def test_provider_accepts_hostname_resolving_only_to_public_addresses(
        self, monkeypatch
    ):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        )

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://provider.example/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        assert provider._request_url == "https://provider.example/v1/chat/completions"

    def test_provider_accepts_hostname_resolving_to_proxy_fake_ip(self, monkeypatch):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.34", 443))
            ],
        )

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://provider.example/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        assert provider._request_url == "https://provider.example/v1/chat/completions"

    def test_provider_rejects_proxy_fake_ip_when_used_as_literal_endpoint(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        with pytest.raises(ValueError, match="内网地址"):
            OpenAICompatibleDraftProvider(
                endpoint_url="https://198.18.0.34/v1",
                model="draft-model",
                api_key="unit-test-secret",
            )

    def test_provider_rechecks_dns_before_opening_connection(self, monkeypatch):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        )
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://provider.example/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        opener = StubOpener(FakeJsonResponse({}))
        setattr(provider, "_opener", opener)
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ],
        )

        with pytest.raises(ValueError, match="内网地址"):
            provider(single_user_message("显示提醒"), {"triggers": {}, "actions": {}})
        assert opener.calls == []

    def test_chat_provider_sends_tool_payload_and_returns_rule_draft(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        selector = ai_selector(
            name="打开网站",
            trigger_type="usb_insert",
            action_type="open_url",
            action_params={"url": "https://example.com"},
        )
        response_payload = {
            "choices": [{"message": {"tool_calls": [{
                "function": {
                    "name": "propose_rule_draft",
                    "arguments": json.dumps(selector),
                },
            }]}}],
        }

        schema = {
            "triggers": {"usb_insert": {"name": "U 盘插入"}},
            "actions": {"open_url": {"name": "打开网址", "params": [{
                "name": "url", "label": "网址", "type": "string",
            }]}},
        }
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        opener = StubOpener(FakeJsonResponse(response_payload))
        setattr(provider, "_opener", opener)

        result = provider(single_user_message("插入 U 盘后打开网站"), schema)

        assert result == {
            "ok": True,
            "result_type": "rule_draft",
            "candidates": [
                {
                    "name": "打开网站",
                    "event": {"type": "usb_insert", "params": {}},
                    "actions": [{
                        "type": "open_url",
                        "params": {"url": "https://example.com"},
                    }],
                }
            ],
        }
        request, timeout = opener.calls[0]
        assert request.full_url == "https://example.invalid/v1/chat/completions"
        assert request.get_header("Authorization") == "Bearer unit-test-secret"
        assert request.get_header("Content-type") == "application/json"
        assert timeout == 120
        assert b"unit-test-secret" not in request.data
        body = json.loads(request.data)
        assert body["tool_choice"] == "auto"
        assert _request_tool_names(body) == {"propose_rule_draft", "propose_plugin", "reply"}
        system_prompt = body["messages"][0]["content"]
        assert "usb_insert" in system_prompt
        assert "U 盘插入" in system_prompt
        assert "url" in system_prompt
        assert "propose_rule_draft" in system_prompt
        assert "只输出一个 JSON" not in system_prompt
        assert "一类任务" in system_prompt
        assert "run(meta, config, emit_event, shutdown_event)" in system_prompt
        assert "仅供审查" in system_prompt

        with pytest.raises(ValueError, match="HTTPS") as https_error:
            OpenAICompatibleDraftProvider(
                endpoint_url="http://example.invalid/v1",
                model="draft-model",
                api_key="unit-test-secret",
            )
        assert "unit-test-secret" not in str(https_error.value)

        malformed_payload = b"not-json"
        setattr(provider, "_opener", StubOpener(FakeJsonResponse(malformed_payload)))
        with pytest.raises(ValueError) as malformed_error:
            provider(single_user_message("插入 U 盘后打开网站"), schema)
        assert "unit-test-secret" not in str(malformed_error.value)

    def test_disabled_returns_403_without_calling_provider_or_saving(
        self, api_env, monkeypatch
    ):
        provider = RecordingProvider(candidates=[ai_candidate()])
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)
        saved = []
        monkeypatch.setattr(
            api_env.api, "_save_rules", lambda rules: saved.append(rules) or True
        )

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("插入 U 盘时打开示例网站")},
            headers=api_env.headers,
        )

        assert response.status_code == 403
        body = response.json()
        assert body["ok"] is False
        assert body["code"] == "ai_disabled"
        assert provider.calls == []
        assert saved == []

    def test_enabled_produces_validated_ai_draft_without_saving_or_running(
        self, api_env, monkeypatch
    ):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = RecordingProvider(candidates=[ai_candidate()])
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)
        saved = []
        monkeypatch.setattr(
            api_env.api, "_save_rules", lambda rules: saved.append(rules) or True
        )
        engine = FakeEngine()
        api_env.api._engine_ref = engine

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("插入 U 盘时打开示例网站")},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["source"] == "ai"
        draft = body["draft"]
        assert draft["name"] == "AI 草稿规则"
        assert draft["event"]["type"] == "usb_insert"
        assert draft["actions"][0]["type"] == "open_url"
        validation = body["validation"]
        assert validation["ok"] is True
        assert validation["valid"] is True
        assert validation["summary"]["errors"] == 0
        call = provider.calls[0]
        assert call["messages"] == [{"role": "user", "content": "插入 U 盘时打开示例网站"}]
        assert call["allow_plugin_source"] is False
        assert saved == []
        assert engine.calls == []
        assert api_env.api._load_rules() == []

    @pytest.mark.parametrize(
        "provider",
        [
            RecordingProvider(
                error="upstream https://example.invalid/v1 refused key super-secret-token-123"
            ),
            RecordingProvider(candidates="not-a-list"),
        ],
        ids=["provider_raises", "malformed_candidates"],
    )
    def test_bad_provider_output_is_rejected_without_secret_in_error(
        self, api_env, monkeypatch, provider
    ):
        assert api_env.api._save_config(enabled_ai_config()) is True
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)
        saved = []
        monkeypatch.setattr(
            api_env.api, "_save_rules", lambda rules: saved.append(rules) or True
        )

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("插入 U 盘时打开示例网站")},
            headers=api_env.headers,
        )

        assert response.status_code == 502
        body = response.json()
        assert body["ok"] is False
        assert "code" in body
        assert "super-secret-token-123" not in response.text
        assert "example.invalid" not in response.text
        assert saved == []

    @pytest.mark.parametrize(
        ("api_format", "endpoint_url", "response_payload"),
        [
            (
                "chat_completions",
                "https://example.invalid/v1/chat/completions",
                {"choices": [{"message": {"tool_calls": [{
                    "function": {
                        "name": "propose_rule_draft",
                        "arguments": json.dumps(ai_selector()),
                    },
                }]}}]},
            ),
            (
                "responses",
                "https://example.invalid/v1/responses",
                {"output": [{
                    "type": "function_call",
                    "name": "propose_rule_draft",
                    "arguments": json.dumps(ai_selector()),
                    "call_id": "call_path_1",
                }]},
            ),
        ],
    )
    def test_full_api_path_is_not_appended_twice(
        self, api_format, endpoint_url, response_payload
    ):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        opener = StubOpener(FakeJsonResponse(response_payload))
        provider = OpenAICompatibleDraftProvider(
            endpoint_url=endpoint_url,
            model="draft-model",
            api_key="unit-test-secret",
            api_format=api_format,
        )
        setattr(provider, "_opener", opener)

        provider(single_user_message("每天九点提醒我"), {
            "triggers": {"time_schedule": {"name": "定时"}},
            "actions": {"notify": {"name": "显示通知"}},
        })

        assert opener.calls[0][0].full_url == endpoint_url


    def test_real_provider_uses_saved_key_when_request_has_none(
        self, api_env, monkeypatch, fake_store
    ):
        fake_store["saved"] = "sk-saved-fallback-444"
        fake_store["status"] = store.KeyStoreStatus.STORED
        captured = {}

        class CapturingProvider:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __call__(self, messages, schema, *, allow_plugin_source=False):
                return {"ok": True, "candidates": [ai_candidate()]}

        monkeypatch.setattr(
            api_server, "OpenAICompatibleDraftProvider", CapturingProvider
        )
        assert api_env.api._save_config(enabled_ai_config()) is True

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("插入 U 盘时打开示例网站")},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert captured["api_key"] == "sk-saved-fallback-444"
        assert "sk-saved-fallback-444" not in response.text

    def test_real_provider_prefers_request_key_over_saved(
        self, api_env, monkeypatch, fake_store
    ):
        fake_store["saved"] = "sk-saved-ignored-555"
        fake_store["status"] = store.KeyStoreStatus.STORED
        captured = {}

        class CapturingProvider:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __call__(self, messages, schema, *, allow_plugin_source=False):
                return {"ok": True, "candidates": [ai_candidate()]}

        monkeypatch.setattr(
            api_server, "OpenAICompatibleDraftProvider", CapturingProvider
        )
        assert api_env.api._save_config(enabled_ai_config()) is True

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={
                "messages": single_user_message("插入 U 盘时打开示例网站"),
                "api_key": "sk-request-666",
            },
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert captured["api_key"] == "sk-request-666"
        assert "sk-request-666" not in response.text

    def test_real_provider_returns_502_when_no_key_available(
        self, api_env, monkeypatch, fake_store
    ):
        constructed = []

        class MustNotConstruct:
            def __init__(self, **kwargs):
                constructed.append(kwargs)

        monkeypatch.setattr(
            api_server, "OpenAICompatibleDraftProvider", MustNotConstruct
        )
        assert api_env.api._save_config(enabled_ai_config()) is True

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("插入 U 盘时打开示例网站")},
            headers=api_env.headers,
        )

        assert response.status_code == 502
        assert response.json()["code"] == "ai_provider_failed"
        assert constructed == []

    def test_injected_provider_never_touches_key_store(
        self, api_env, monkeypatch, fake_store
    ):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = RecordingProvider(candidates=[ai_candidate()])
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={
                "messages": single_user_message("插入 U 盘时打开示例网站"),
                "api_key": "sk-session-777",
            },
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert fake_store["load_calls"] == []
        assert provider.calls[0]["messages"] == [
            {"role": "user", "content": "插入 U 盘时打开示例网站"},
        ]


class TestAiResultDispatch:
    def test_plugin_proposal_returns_metadata_only_without_side_effects(
        self, api_env, monkeypatch
    ):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = RecordingProvider(proposal=plugin_proposal_args())
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)
        saved = []
        monkeypatch.setattr(
            api_env.api, "_save_rules", lambda rules: saved.append(rules) or True
        )
        sign_calls = []
        monkeypatch.setattr(
            api_env.api,
            "_counter_sign_author_key",
            lambda plugin_dir, password: sign_calls.append((plugin_dir, password)),
        )
        signature_calls = []
        monkeypatch.setattr(
            api_server,
            "plugin_signature_kind",
            lambda *args, **kwargs: signature_calls.append(args),
        )
        engine = FakeEngine()
        api_env.api._engine_ref = engine
        config_before = (api_env.tmp_path / "config.json").read_text(encoding="utf-8")

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("需要一个能发 webhook 通知的动作")},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "ok": True,
            "source": "ai",
            "result_type": "plugin_proposal",
            "proposal": plugin_proposal_args(),
        }
        assert "draft" not in body
        assert "validation" not in body
        assert saved == []
        assert sign_calls == []
        assert signature_calls == []
        assert engine.calls == []
        assert api_env.api._load_rules() == []
        assert not (api_env.tmp_path / "user_plugins").exists()
        assert (
            api_env.tmp_path / "config.json"
        ).read_text(encoding="utf-8") == config_before

    @pytest.mark.parametrize(
        ("proposal", "leak"),
        [
            (dict(plugin_proposal_args(), source_code="print('pwned')"), "print('pwned')"),
            (
                dict(plugin_proposal_args(), permissions=["steal_everything"]),
                "steal_everything",
            ),
            (dict(plugin_proposal_args(), id="bad id!"), "bad id!"),
            (dict(plugin_proposal_args(), kind="exploit"), "exploit"),
            ("not-a-dict", None),
        ],
        ids=[
            "forbidden_field",
            "unknown_permission",
            "invalid_id",
            "invalid_kind",
            "not_a_dict",
        ],
    )
    def test_malicious_proposal_rejected_generically_without_echo(
        self, api_env, monkeypatch, proposal, leak
    ):
        assert api_env.api._save_config(enabled_ai_config()) is True
        monkeypatch.setattr(
            api_env.api,
            "ai_draft_provider",
            RecordingProvider(proposal=proposal),
            raising=False,
        )
        saved = []
        monkeypatch.setattr(
            api_env.api, "_save_rules", lambda rules: saved.append(rules) or True
        )

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("需要一个危险的动作")},
            headers=api_env.headers,
        )

        assert response.status_code == 502
        body = response.json()
        assert body["ok"] is False
        assert body["code"] == "ai_provider_failed"
        if leak:
            assert leak not in response.text
        assert saved == []
        assert api_env.api._load_rules() == []

    def test_unknown_result_type_returns_502(self, api_env, monkeypatch):
        assert api_env.api._save_config(enabled_ai_config()) is True

        class UnknownTypeProvider:
            def __call__(self, messages, schema, allow_plugin_source=False):
                return {
                    "ok": True,
                    "result_type": "run_code",
                    "candidates": [ai_candidate()],
                }

        monkeypatch.setattr(
            api_env.api, "ai_draft_provider", UnknownTypeProvider(), raising=False
        )

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("执行任意代码")},
            headers=api_env.headers,
        )

        assert response.status_code == 502
        body = response.json()
        assert body["ok"] is False
        assert body["code"] == "ai_provider_failed"

    def test_explicit_rule_draft_result_type_still_returns_draft(
        self, api_env, monkeypatch
    ):
        assert api_env.api._save_config(enabled_ai_config()) is True

        class TypedRuleProvider:
            def __call__(self, messages, schema, allow_plugin_source=False):
                return {
                    "ok": True,
                    "result_type": "rule_draft",
                    "candidates": [ai_candidate()],
                }

        monkeypatch.setattr(
            api_env.api, "ai_draft_provider", TypedRuleProvider(), raising=False
        )

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("插入 U 盘时打开示例网站")},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["source"] == "ai"
        assert body["result_type"] == "rule_draft"
        assert body["draft"]["name"] == "AI 草稿规则"
        assert body["validation"]["ok"] is True


class TestMultiTurnMessages:
    def test_full_messages_reach_injected_provider(self, api_env, monkeypatch):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = RecordingProvider(candidates=[ai_candidate()])
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)
        messages = [
            {"role": "user", "content": "第一条"},
            {"role": "assistant", "content": "我先澄清一下"},
            {"role": "user", "content": "插入 U 盘时打开网站"},
        ]

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": messages},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert provider.calls[0]["messages"] == messages
        assert provider.calls[0]["allow_plugin_source"] is False

    def test_full_messages_reach_real_provider(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        response_payload = {"choices": [{"message": {"tool_calls": [{
            "function": {
                "name": "propose_rule_draft",
                "arguments": json.dumps(ai_selector()),
            },
        }]}}]}
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        opener = StubOpener(FakeJsonResponse(response_payload))
        setattr(provider, "_opener", opener)
        messages = [
            {"role": "user", "content": "第一条"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "提醒我喝水"},
        ]

        provider(messages, {
            "triggers": {"time_schedule": {"name": "定时"}},
            "actions": {"notify": {"name": "显示通知"}},
        })

        sent = json.loads(opener.calls[0][0].data)["messages"]
        assert sent[0]["role"] == "system"
        assert [m for m in sent if m["role"] == "system"] == [sent[0]]
        assert sent[1:] == messages

    def test_system_prompt_contains_catalog_and_authoring_guidance(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        response_payload = {"choices": [{"message": {"tool_calls": [{
            "function": {
                "name": "propose_rule_draft",
                "arguments": json.dumps(ai_selector()),
            },
        }]}}]}
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        opener = StubOpener(FakeJsonResponse(response_payload))
        setattr(provider, "_opener", opener)

        provider(single_user_message("提醒我"), {
            "triggers": {"time_schedule": {"name": "定时"}},
            "actions": {"notify": {"name": "显示通知"}},
        })

        system = json.loads(opener.calls[0][0].data)["messages"][0]["content"]
        assert "time_schedule" in system  # 目录
        assert "一类任务" in system  # 泛化
        assert "run(meta, config, emit_event, shutdown_event)" in system  # 文档
        assert "仅供审查" in system  # 源码警告

    def test_missing_messages_rejected(self, api_env):
        assert api_env.api._save_config(enabled_ai_config()) is True
        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"description": "旧的 description 字段"},
            headers=api_env.headers,
        )

        assert response.status_code == 400
        assert response.json()["ok"] is False

    @pytest.mark.parametrize(
        "messages",
        [
            None,
            [],
            "not-a-list",
            [{"role": "system", "content": "hi"}],
            [{"role": "user", "content": ""}],
            [{"role": "user", "content": "   "}],
            [{"role": "user", "content": 123}],
            [{"role": "user", "content": "x" * 4001}],
            [{"role": "user", "content": "hi", "extra": 1}],
            [{"role": "assistant", "content": "hi"}],
            [{"role": "user", "content": "hi"}] * 41,
        ],
        ids=[
            "missing",
            "empty",
            "not_a_list",
            "bad_role",
            "empty_content",
            "whitespace_content",
            "non_string_content",
            "content_too_long",
            "extra_key",
            "last_role_not_user",
            "too_many_messages",
        ],
    )
    def test_malformed_messages_rejected(self, api_env, messages):
        assert api_env.api._save_config(enabled_ai_config()) is True
        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": messages},
            headers=api_env.headers,
        )

        assert response.status_code == 400
        assert response.json()["ok"] is False

    @pytest.mark.parametrize(
        "consent",
        [
            "not-an-object",
            [],
            {},
            {"plugin_id": ""},
            {"plugin_id": 123},
            {"plugin_id": "../evil"},
            {"plugin_id": "ok_id", "extra": True},
        ],
        ids=[
            "not_an_object",
            "list",
            "missing_plugin_id",
            "empty_id",
            "non_string_id",
            "path_traversal_id",
            "extra_key",
        ],
    )
    def test_invalid_consent_rejected(self, api_env, consent):
        assert api_env.api._save_config(enabled_ai_config()) is True
        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={
                "messages": single_user_message("生成源码"),
                "consent": consent,
            },
            headers=api_env.headers,
        )

        assert response.status_code == 400
        assert response.json()["ok"] is False


class TestReplyAndPluginSource:
    @pytest.mark.parametrize(
        ("api_format", "body"),
        [
            (
                "chat_completions",
                {"choices": [{"message": {"tool_calls": [{
                    "function": {
                        "name": "reply",
                        "arguments": json.dumps({"message": "我可以帮你起草规则。"}),
                    },
                }]}}]},
            ),
            (
                "responses",
                {"output": [{
                    "type": "function_call",
                    "name": "reply",
                    "arguments": json.dumps({"message": "我可以帮你起草规则。"}),
                    "call_id": "call_reply_1",
                }]},
            ),
        ],
    )
    def test_reply_tool_dispatches_assistant_message(self, api_format, body):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format=api_format,
        )
        setattr(provider, "_opener", StubOpener(FakeJsonResponse(body)))

        result = provider(single_user_message("你能做什么"), {
            "triggers": {"time_schedule": {"name": "定时"}},
            "actions": {"notify": {"name": "显示通知"}},
        })

        assert result == {
            "ok": True,
            "result_type": "assistant_message",
            "message": "我可以帮你起草规则。",
        }

    def test_source_tool_absent_without_consent(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        response_payload = {"choices": [{"message": {"tool_calls": [{
            "function": {
                "name": "reply",
                "arguments": json.dumps({"message": "好的"}),
            },
        }]}}]}
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        opener = StubOpener(FakeJsonResponse(response_payload))
        setattr(provider, "_opener", opener)

        provider(single_user_message("生成源码"), {"triggers": {}, "actions": {}})

        body = json.loads(opener.calls[0][0].data)
        assert "propose_plugin_source" not in _request_tool_names(body)

    def test_source_tool_present_with_consent(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        response_payload = {"choices": [{"message": {"tool_calls": [{
            "function": {
                "name": "reply",
                "arguments": json.dumps({"message": "好的"}),
            },
        }]}}]}
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        opener = StubOpener(FakeJsonResponse(response_payload))
        setattr(provider, "_opener", opener)

        provider(
            single_user_message("生成源码"),
            {"triggers": {}, "actions": {}},
            allow_plugin_source=True,
        )

        body = json.loads(opener.calls[0][0].data)
        assert "propose_plugin_source" in _request_tool_names(body)

    @pytest.mark.parametrize(
        ("api_format", "body"),
        [
            (
                "chat_completions",
                {"choices": [{"message": {"tool_calls": [{
                    "function": {
                        "name": "propose_plugin_source",
                        "arguments": json.dumps(plugin_source_args()),
                    },
                }]}}]},
            ),
            (
                "responses",
                {"output": [{
                    "type": "function_call",
                    "name": "propose_plugin_source",
                    "arguments": json.dumps(plugin_source_args()),
                    "call_id": "call_src_1",
                }]},
            ),
        ],
    )
    def test_source_tool_dispatch_returns_plugin_source_fields(self, api_format, body):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format=api_format,
        )
        setattr(provider, "_opener", StubOpener(FakeJsonResponse(body)))

        result = provider(
            single_user_message("生成源码"),
            {"triggers": {}, "actions": {}},
            allow_plugin_source=True,
        )

        assert result == {
            "ok": True,
            "result_type": "plugin_source",
            "kind": plugin_source_args()["kind"],
            "manifest": plugin_source_args()["manifest"],
            "source": plugin_source_args()["source"],
        }

    def _source_result(self, **manifest_overrides):
        return {
            "ok": True,
            "result_type": "plugin_source",
            **plugin_source_args(**manifest_overrides),
        }

    def test_plugin_source_without_consent_returns_consent_required(
        self, api_env, monkeypatch
    ):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = RecordingProvider(result=self._source_result())
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("生成源码")},
            headers=api_env.headers,
        )

        assert response.status_code == 409
        body = response.json()
        assert body["ok"] is False
        assert body["code"] == "consent_required"
        assert provider.calls[0]["allow_plugin_source"] is False

    def test_plugin_source_with_consent_returns_reviewed_plugin(
        self, api_env, monkeypatch
    ):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = RecordingProvider(result=self._source_result())
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={
                "messages": single_user_message("生成源码"),
                "consent": {"plugin_id": "notify_world"},
            },
            headers=api_env.headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["source"] == "ai"
        assert body["result_type"] == "plugin_source"
        assert body["plugin"]["id"] == "notify_world"
        assert body["plugin"]["kind"] == "action"
        assert body["plugin"]["source"] == plugin_source_args()["source"]
        assert "findings" in body["plugin"]
        assert provider.calls[0]["allow_plugin_source"] is True

    def test_plugin_source_consent_id_mismatch_rejected(self, api_env, monkeypatch):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = RecordingProvider(
            result=self._source_result(manifest={
                "id": "other_id",
                "name": "别的",
                "description": "别的东西",
                "enabled": True,
                "version_code": 1,
                "version": "1.0",
                "package_name": "io.github.notmyfault.other",
            })
        )
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={
                "messages": single_user_message("生成源码"),
                "consent": {"plugin_id": "notify_world"},
            },
            headers=api_env.headers,
        )

        assert response.status_code == 502
        body = response.json()
        assert body["ok"] is False
        assert body["code"] == "ai_provider_failed"
        assert "def run" not in response.text
        assert "other_id" not in response.text

    def test_plugin_source_with_consent_has_no_side_effects(
        self, api_env, monkeypatch
    ):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = RecordingProvider(result=self._source_result())
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)
        saved = []
        monkeypatch.setattr(
            api_env.api, "_save_rules", lambda rules: saved.append(rules) or True
        )
        sign_calls = []
        monkeypatch.setattr(
            api_env.api,
            "_counter_sign_author_key",
            lambda plugin_dir, password: sign_calls.append((plugin_dir, password)),
        )
        signature_calls = []
        monkeypatch.setattr(
            api_server,
            "plugin_signature_kind",
            lambda *args, **kwargs: signature_calls.append(args),
        )
        engine = FakeEngine()
        api_env.api._engine_ref = engine
        config_before = (api_env.tmp_path / "config.json").read_text(encoding="utf-8")

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={
                "messages": single_user_message("生成源码"),
                "consent": {"plugin_id": "notify_world"},
            },
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert saved == []
        assert sign_calls == []
        assert signature_calls == []
        assert engine.calls == []
        assert api_env.api._load_rules() == []
        assert not (api_env.tmp_path / "user_plugins").exists()
        assert (
            api_env.tmp_path / "config.json"
        ).read_text(encoding="utf-8") == config_before

    @pytest.mark.parametrize(
        "message",
        ["我可以帮你起草规则。", "x" * 4000],
        ids=["normal", "at_boundary"],
    )
    def test_assistant_message_returns_to_client(self, api_env, monkeypatch, message):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = RecordingProvider(result={
            "ok": True,
            "result_type": "assistant_message",
            "message": message,
        })
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("帮我澄清一下")},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "source": "ai",
            "result_type": "assistant_message",
            "message": message,
        }

    @pytest.mark.parametrize(
        "message",
        ["", "   ", "x" * 4001, 123],
        ids=["empty", "whitespace", "oversized", "non_string"],
    )
    def test_empty_or_oversized_assistant_response_rejected(
        self, api_env, monkeypatch, message
    ):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = RecordingProvider(result={
            "ok": True,
            "result_type": "assistant_message",
            "message": message,
        })
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)

        response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("帮我澄清一下")},
            headers=api_env.headers,
        )

        assert response.status_code == 502
        body = response.json()
        assert body["ok"] is False
        assert body["code"] == "ai_provider_failed"


class TestEarlyApproval:
    def test_strict_high_risk_returns_admin_key_required_and_persists_nothing(
        self, api_env, monkeypatch
    ):
        from notmyfault.security import rule_approval
        from notmyfault.security.security import SecurityMode

        monkeypatch.setattr(
            rule_approval, "detect_security_mode", lambda: SecurityMode.STRICT
        )
        monkeypatch.setattr(
            rule_approval, "key_status", lambda: {"exists": True, "encrypted": True}
        )
        api_env.api._get_plugins_schema = lambda: {
            "triggers": {"usb_insert": {"permissions": []}},
            "actions": {"bluetooth_toggle": {"permissions": ["admin"]}},
        }
        saved = []
        monkeypatch.setattr(
            api_env.api, "_save_rules", lambda rules: saved.append(rules) or True
        )
        rule = {
            "name": "管理员规则",
            "event": {"type": "usb_insert", "params": {}},
            "actions": [{"type": "bluetooth_toggle", "params": {}}],
        }

        response = api_env.client.post(
            "/api/rules/approve",
            json={"rules": [rule]},
            headers=api_env.headers,
        )

        assert response.status_code == 409
        body = response.json()
        assert body["ok"] is False
        assert body["code"] == "admin_key_required"
        assert body["plugins"] == ["bluetooth_toggle"]
        assert saved == []
        assert api_env.api._load_rules() == []


class TestAiDraftingSettings:
    def test_read_returns_non_secret_fields_only(self, api_env):
        assert api_env.api._save_config({
            "settings": {
                "ai_drafting": {
                    "enabled": True,
                    "endpoint_url": "https://example.invalid/v1",
                    "model": "test-model",
                    "api_key": "stored-secret-value",
                }
            }
        }) is True

        response = api_env.client.get(
            "/api/settings/ai-drafting", headers=api_env.headers
        )

        assert response.status_code == 200
        assert response.json() == {
            "enabled": True,
            "endpoint_url": "https://example.invalid/v1",
            "model": "test-model",
            "api_format": "chat_completions",
            "api_key_status": "unsupported",
        }
        assert "stored-secret-value" not in response.text

    def test_update_never_writes_secret_to_disk(self, api_env):
        response = api_env.client.put(
            "/api/settings/ai-drafting",
            json={
                "enabled": True,
                "endpoint_url": "https://example.invalid/v1",
                "model": "test-model",
                "api_key": "attacker-secret-value",
            },
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert "attacker-secret-value" not in response.text
        raw = (api_env.tmp_path / "config.json").read_text(encoding="utf-8")
        assert "attacker-secret-value" not in raw
        stored = api_env.api._load_config()["settings"]["ai_drafting"]
        assert stored == {
            "enabled": True,
            "endpoint_url": "https://example.invalid/v1",
            "model": "test-model",
            "api_format": "chat_completions",
        }


class TestAiApiKeySettings:
    """保存的 AI API key 的状态、保存与删除端点契约。"""

    @pytest.mark.parametrize(
        ("status", "label"),
        [
            (store.KeyStoreStatus.STORED, "saved"),
            (store.KeyStoreStatus.ABSENT, "none"),
            (store.KeyStoreStatus.UNSUPPORTED, "unsupported"),
            (store.KeyStoreStatus.CORRUPT, "corrupt"),
        ],
    )
    def test_read_maps_status_without_revealing_key(
        self, api_env, fake_store, status, label
    ):
        fake_store["status"] = status
        fake_store["saved"] = "sk-status-secret-987"

        response = api_env.client.get(
            "/api/settings/ai-drafting", headers=api_env.headers
        )

        assert response.status_code == 200
        body = response.json()
        assert body["api_key_status"] == label
        assert "sk-status-secret-987" not in response.text
        assert "api_key" not in body

    def test_put_saves_key_and_returns_status_only(self, api_env, fake_store):
        assert api_env.api._save_config(enabled_ai_config()) is True

        response = api_env.client.put(
            "/api/settings/ai-drafting/api-key",
            json={"api_key": "sk-saved-secret-111"},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True, "api_key_status": "saved"}
        assert fake_store["save_calls"] == ["sk-saved-secret-111"]
        assert "sk-saved-secret-111" not in response.text
        raw = (api_env.tmp_path / "config.json").read_text(encoding="utf-8")
        assert "sk-saved-secret-111" not in raw

    def test_put_replaces_existing_key(self, api_env, fake_store):
        fake_store["saved"] = "sk-old-secret"
        fake_store["status"] = store.KeyStoreStatus.STORED

        response = api_env.client.put(
            "/api/settings/ai-drafting/api-key",
            json={"api_key": "sk-new-secret-222"},
            headers=api_env.headers,
        )

        assert response.status_code == 200
        assert fake_store["saved"] == "sk-new-secret-222"
        assert fake_store["save_calls"] == ["sk-new-secret-222"]

    @pytest.mark.parametrize(
        "payload",
        [{}, {"api_key": ""}, {"api_key": "   "}, {"api_key": 123}],
        ids=["missing", "empty", "whitespace_only", "not_a_string"],
    )
    def test_put_blank_key_returns_400_without_saving(
        self, api_env, fake_store, payload
    ):
        response = api_env.client.put(
            "/api/settings/ai-drafting/api-key",
            json=payload,
            headers=api_env.headers,
        )

        assert response.status_code == 400
        assert response.json()["ok"] is False
        assert fake_store["save_calls"] == []

    def test_put_unsupported_platform_returns_409(self, api_env, monkeypatch):
        def unsupported_save(key):
            raise store.KeyStoreUnsupportedError("当前平台不支持 DPAPI 密钥存储")

        monkeypatch.setattr(api_server.api_key_store, "save_api_key", unsupported_save)

        response = api_env.client.put(
            "/api/settings/ai-drafting/api-key",
            json={"api_key": "sk-wont-be-saved"},
            headers=api_env.headers,
        )

        assert response.status_code == 409
        assert "sk-wont-be-saved" not in response.text

    def test_put_store_failure_returns_generic_500_without_leak(
        self, api_env, monkeypatch
    ):
        def failing_save(key):
            raise store.KeyStoreError(f"DPAPI 加密失败，key={key}")

        monkeypatch.setattr(api_server.api_key_store, "save_api_key", failing_save)

        response = api_env.client.put(
            "/api/settings/ai-drafting/api-key",
            json={"api_key": "sk-leak-check-333"},
            headers=api_env.headers,
        )

        assert response.status_code == 500
        assert response.json()["ok"] is False
        assert "sk-leak-check-333" not in response.text
        assert "DPAPI" not in response.text

    def test_delete_is_idempotent(self, api_env, fake_store):
        fake_store["saved"] = "sk-to-delete"
        fake_store["status"] = store.KeyStoreStatus.STORED

        first = api_env.client.delete(
            "/api/settings/ai-drafting/api-key", headers=api_env.headers
        )
        second = api_env.client.delete(
            "/api/settings/ai-drafting/api-key", headers=api_env.headers
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == {"ok": True, "api_key_status": "none"}
        assert second.json() == {"ok": True, "api_key_status": "none"}
        assert fake_store["delete_calls"] == [True, True]
        assert fake_store["saved"] is None

    def test_delete_store_failure_returns_generic_500(self, api_env, monkeypatch):
        def failing_delete():
            raise store.KeyStoreError("无法删除密钥文件 sk-delete-leak")

        monkeypatch.setattr(api_server.api_key_store, "delete_api_key", failing_delete)

        response = api_env.client.delete(
            "/api/settings/ai-drafting/api-key", headers=api_env.headers
        )

        assert response.status_code == 500
        assert "sk-delete-leak" not in response.text


class FakeSseResponse:
    """把 SSE 字节块伪装成 opener 拿到的流式响应，逐块交给 read。"""

    def __init__(self, chunks, exc=None):
        self._chunks = list(chunks)
        self._exc = exc
        self.read_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        self.read_calls += 1
        if self._exc is not None:
            exc = self._exc
            self._exc = None
            raise exc
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def sse_data(payload):
    """把事件对象编成一条上游 SSE data 帧；None 表示 [DONE]。"""
    if payload is None:
        return b"data: [DONE]\n\n"
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def parse_sse_events(text):
    """把整段下游 SSE 响应拆成 (event_name, payload) 列表。"""
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if name is not None:
            events.append((name, json.loads(data) if data else None))
    return events


def _strip_binding_ids(value):
    """递归剥掉 binding_id，只留每次请求都稳定的字段。"""
    if isinstance(value, dict):
        return {
            key: _strip_binding_ids(item)
            for key, item in value.items()
            if key != "binding_id"
        }
    if isinstance(value, list):
        return [_strip_binding_ids(item) for item in value]
    return value


class StreamingProvider:
    """假流式供应商：stream() 产出 (kind, payload)，遇 Exception 项就抛出。"""

    def __init__(self, events):
        self.events = list(events)
        self.calls = []

    def stream(self, messages, schema, allow_plugin_source=False):
        self.calls.append({
            "messages": messages,
            "schema": schema,
            "allow_plugin_source": allow_plugin_source,
        })
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


class TestAiStreamProvider:
    def test_chat_stream_emits_deltas_and_reconstructs_tool_call(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        selector = ai_selector()
        arguments = json.dumps(selector, ensure_ascii=False)
        split = len(arguments) // 2
        chunks = [
            sse_data({"choices": [{"delta": {"reasoning_content": "先想", "content": ""}}]}),
            sse_data({"choices": [{"delta": {"content": "好的，"}}]}),
            sse_data({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_1",
                "function": {"name": "propose_rule_", "arguments": ""},
            }]}}]}),
            sse_data({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"name": "draft", "arguments": arguments[:split]},
            }]}}]}),
            sse_data({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": arguments[split:]},
            }]}}]}),
            sse_data(None),
        ]
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format="chat_completions",
        )
        opener = StubOpener(FakeSseResponse(chunks))
        setattr(provider, "_opener", opener)

        events = list(provider.stream(single_user_message("每天九点提醒我"), {
            "triggers": {"time_schedule": {"name": "定时"}},
            "actions": {"notify": {"name": "显示通知"}},
        }))

        kinds = [kind for kind, _payload in events]
        assert ("reasoning", "先想") in events
        assert ("text", "好的，") in events
        assert kinds[-1] == "result"
        result = events[-1][1]
        assert result["result_type"] == "rule_draft"
        assert result["candidates"][0]["event"]["type"] == "time_schedule"
        assert result["candidates"][0]["actions"][0]["type"] == "notify"
        # 流式请求才带 stream 标记。
        assert json.loads(opener.calls[0][0].data)["stream"] is True

    def test_responses_stream_emits_deltas_and_reconstructs_tool_call(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        selector = ai_selector()
        arguments = json.dumps(selector, ensure_ascii=False)
        split = len(arguments) // 2
        chunks = [
            sse_data({"type": "response.created", "response": {}}),
            sse_data({"type": "response.reasoning_summary_text.delta", "delta": "先想"}),
            sse_data({"type": "response.output_text.delta", "delta": "好的，"}),
            sse_data({"type": "response.output_item.added", "item": {
                "type": "function_call", "name": "propose_rule_draft", "call_id": "call_resp_1",
            }}),
            sse_data({"type": "response.function_call_arguments.delta", "delta": arguments[:split]}),
            sse_data({"type": "response.function_call_arguments.delta", "delta": arguments[split:]}),
            sse_data({"type": "response.output_item.done", "item": {
                "type": "function_call", "name": "propose_rule_draft", "call_id": "call_resp_1",
                "arguments": arguments,
            }}),
            sse_data(None),
        ]
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format="responses",
        )
        setattr(provider, "_opener", StubOpener(FakeSseResponse(chunks)))

        events = list(provider.stream(single_user_message("每天九点提醒我"), {
            "triggers": {"time_schedule": {"name": "定时"}},
            "actions": {"notify": {"name": "显示通知"}},
        }))

        assert ("reasoning", "先想") in events
        assert ("text", "好的，") in events
        result = events[-1][1]
        assert result["result_type"] == "rule_draft"
        assert result["candidates"][0]["event"]["type"] == "time_schedule"

    def test_responses_stream_retains_completed_response(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        selector = ai_selector()
        arguments = json.dumps(selector, ensure_ascii=False)
        chunks = [
            sse_data({"type": "response.output_text.delta", "delta": "好的，"}),
            sse_data({"type": "response.completed", "response": {
                "output": [{
                    "type": "function_call",
                    "name": "propose_rule_draft",
                    "arguments": arguments,
                    "call_id": "call_done_1",
                }],
            }}),
            sse_data(None),
        ]
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format="responses",
        )
        setattr(provider, "_opener", StubOpener(FakeSseResponse(chunks)))

        events = list(provider.stream(single_user_message("每天九点提醒我"), {
            "triggers": {"time_schedule": {"name": "定时"}},
            "actions": {"notify": {"name": "显示通知"}},
        }))

        result = events[-1][1]
        assert result["result_type"] == "rule_draft"
        assert result["candidates"][0]["event"]["type"] == "time_schedule"

    def test_chat_stream_free_text_falls_back_to_assistant_message(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        chunks = [
            sse_data({"choices": [{"delta": {"content": "我可以"}}]}),
            sse_data({"choices": [{"delta": {"content": "帮你起草规则。"}}]}),
            sse_data(None),
        ]
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
            api_format="chat_completions",
        )
        setattr(provider, "_opener", StubOpener(FakeSseResponse(chunks)))

        events = list(provider.stream(single_user_message("你能做什么"), {
            "triggers": {}, "actions": {},
        }))

        result = events[-1][1]
        assert result == {
            "ok": True,
            "result_type": "assistant_message",
            "message": "我可以帮你起草规则。",
        }

    def test_stream_passes_idle_timeout_to_opener(self):
        from notmyfault.host import ai_provider
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        opener = StubOpener(FakeSseResponse([
            sse_data({"choices": [{"delta": {"content": "好的"}}]}),
            sse_data(None),
        ]))
        setattr(provider, "_opener", opener)

        list(provider.stream(single_user_message("你好"), {"triggers": {}, "actions": {}}))

        assert opener.calls[0][1] == ai_provider._IDLE_TIMEOUT_SECONDS

    def test_stream_timeout_maps_to_value_error(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        setattr(provider, "_opener", StubOpener(FakeSseResponse([], exc=TimeoutError("stalled"))))

        with pytest.raises(ValueError, match="超时"):
            list(provider.stream(single_user_message("你好"), {"triggers": {}, "actions": {}}))

    def test_stream_oversize_body_rejected(self, monkeypatch):
        from notmyfault.host import ai_provider
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        monkeypatch.setattr(ai_provider, "_MAX_RESPONSE_BYTES", 10)
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        setattr(provider, "_opener", StubOpener(FakeSseResponse([
            b"data: " + b"x" * 100 + b"\n\n",
        ])))

        with pytest.raises(ValueError, match="过大"):
            list(provider.stream(single_user_message("你好"), {"triggers": {}, "actions": {}}))

    def test_stream_utf8_split_across_chunks(self):
        from notmyfault.host.ai_provider import OpenAICompatibleDraftProvider

        frame = (
            "data: " + json.dumps({"choices": [{"delta": {"content": "好的"}}]},
                                   ensure_ascii=False) + "\n\n"
        )
        byte_index = len(frame[:frame.index("好")].encode("utf-8"))
        encoded = frame.encode("utf-8")
        chunks = [encoded[:byte_index + 1], encoded[byte_index + 1:]]
        provider = OpenAICompatibleDraftProvider(
            endpoint_url="https://example.invalid/v1",
            model="draft-model",
            api_key="unit-test-secret",
        )
        setattr(provider, "_opener", StubOpener(FakeSseResponse(chunks)))

        events = list(provider.stream(single_user_message("你好"), {"triggers": {}, "actions": {}}))

        assert ("text", "好的") in events


class TestAiStreamEndpoint:
    def test_stream_endpoint_emits_normalized_event_order(self, api_env, monkeypatch):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = StreamingProvider([
            ("reasoning", "先想"),
            ("text", "好的，"),
            ("result", {
                "ok": True, "result_type": "assistant_message",
                "message": "我可以帮你起草规则。",
            }),
        ])
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)

        with api_env.client.stream(
            "POST", "/api/rules/draft/ai/stream",
            json={"messages": single_user_message("每天九点提醒我")},
            headers=api_env.headers,
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            content = b"".join(response.iter_bytes()).decode("utf-8")

        events = parse_sse_events(content)
        assert [name for name, _ in events] == ["status", "reasoning", "text", "result", "done"]
        assert events[0][1] == {"status": "started"}
        assert events[1][1] == {"delta": "先想"}
        assert events[2][1] == {"delta": "好的，"}
        assert events[3][1] == {
            "ok": True, "source": "ai", "result_type": "assistant_message",
            "message": "我可以帮你起草规则。",
        }
        assert events[4][1] == {"status": "done"}
        assert provider.calls[0]["allow_plugin_source"] is False

    def test_stream_endpoint_result_matches_json_endpoint(self, api_env, monkeypatch):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider_result = {"candidates": [ai_candidate()]}

        json_provider = RecordingProvider(result=provider_result)
        monkeypatch.setattr(api_env.api, "ai_draft_provider", json_provider, raising=False)
        json_response = api_env.client.post(
            "/api/rules/draft/ai",
            json={"messages": single_user_message("插入 U 盘时打开网站")},
            headers=api_env.headers,
        )

        stream_provider = StreamingProvider([("result", provider_result)])
        monkeypatch.setattr(api_env.api, "ai_draft_provider", stream_provider, raising=False)
        with api_env.client.stream(
            "POST", "/api/rules/draft/ai/stream",
            json={"messages": single_user_message("插入 U 盘时打开网站")},
            headers=api_env.headers,
        ) as response:
            content = b"".join(response.iter_bytes()).decode("utf-8")

        assert json_response.status_code == 200
        result_events = [p for n, p in parse_sse_events(content) if n == "result"]
        assert len(result_events) == 1
        # binding_id 每次随机生成，比较时剥掉。
        assert _strip_binding_ids(result_events[0]) == _strip_binding_ids(json_response.json())

    def test_stream_endpoint_provider_error_emits_error_then_done(self, api_env, monkeypatch):
        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = StreamingProvider([
            ("reasoning", "先想"),
            RuntimeError("上游挂了"),
        ])
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)

        with api_env.client.stream(
            "POST", "/api/rules/draft/ai/stream",
            json={"messages": single_user_message("每天九点提醒我")},
            headers=api_env.headers,
        ) as response:
            content = b"".join(response.iter_bytes()).decode("utf-8")

        events = parse_sse_events(content)
        assert [name for name, _ in events] == ["status", "reasoning", "error", "done"]
        assert events[2][1] == {"code": "ai_provider_failed", "error": "AI 草稿服务暂不可用"}
        assert events[3][1] == {"status": "done"}

    def test_stream_endpoint_idle_timeout_emits_specific_error(self, api_env, monkeypatch):
        from notmyfault.host.ai_provider import AIProviderIdleTimeoutError

        assert api_env.api._save_config(enabled_ai_config()) is True
        provider = StreamingProvider([AIProviderIdleTimeoutError("AI 草稿请求超时")])
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)

        with api_env.client.stream(
            "POST", "/api/rules/draft/ai/stream",
            json={"messages": single_user_message("每天九点提醒我")},
            headers=api_env.headers,
        ) as response:
            content = b"".join(response.iter_bytes()).decode("utf-8")

        events = parse_sse_events(content)
        assert [name for name, _ in events] == ["status", "error", "done"]
        assert events[1][1] == {
            "code": "idle_timeout",
            "error": "AI 服务长时间没有返回内容",
        }

    def test_stream_endpoint_plugin_source_without_consent_errors(self, api_env, monkeypatch):
        assert api_env.api._save_config(enabled_ai_config()) is True
        source_result = {"ok": True, "result_type": "plugin_source", **plugin_source_args()}
        provider = StreamingProvider([("result", source_result)])
        monkeypatch.setattr(api_env.api, "ai_draft_provider", provider, raising=False)

        with api_env.client.stream(
            "POST", "/api/rules/draft/ai/stream",
            json={"messages": single_user_message("生成源码")},
            headers=api_env.headers,
        ) as response:
            content = b"".join(response.iter_bytes()).decode("utf-8")

        events = parse_sse_events(content)
        assert [name for name, _ in events] == ["status", "error", "done"]
        assert events[1][1] == {"code": "consent_required", "error": "生成插件源码需要用户同意"}

    def test_stream_endpoint_pre_stream_failures_are_plain_json(self, api_env):
        response = api_env.client.post(
            "/api/rules/draft/ai/stream",
            json={"messages": single_user_message("每天九点提醒我")},
            headers=api_env.headers,
        )
        assert response.status_code == 403
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["code"] == "ai_disabled"

        assert api_env.api._save_config(enabled_ai_config()) is True
        response = api_env.client.post(
            "/api/rules/draft/ai/stream",
            json={"messages": []},
            headers=api_env.headers,
        )
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["ok"] is False
