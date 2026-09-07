"""OpenAI 兼容接口的 AI 规则草稿供应商。

模型用白名单里的工具调用回复，供应商解析工具调用并校验参数后返回结果。
"""

import codecs
import json
import socket
import ssl
import threading
from http.client import HTTPException, HTTPSConnection
from typing import Any, Callable, Dict, Iterator
from urllib import parse, request
from urllib.error import HTTPError

from notmyfault.host.ai_rule_draft import parse_rule_draft
from notmyfault.host.ai_skills import build_rule_drafting_skill
from notmyfault.host.ai_tools import (
    SkillSpec,
    ToolCall,
    ToolCallError,
    _require_str,
    chat_tool_definitions,
    extract_chat_text,
    extract_chat_tool_call,
    extract_responses_text,
    extract_responses_tool_call,
    responses_tool_definitions,
)
from notmyfault.security.network import is_private_host, resolve_public_http_url

_REQUEST_TIMEOUT_SECONDS = 120
_MAX_RESPONSE_BYTES = 1024 * 1024
# 流式读每次阻塞等待的上限；上游停住这么久就判超时。
_IDLE_TIMEOUT_SECONDS = 120
_STREAM_CHUNK_BYTES = 8192


class AIProviderIdleTimeoutError(ValueError):
    """AI 流长时间没有返回任何内容。"""


class AIProviderRequestError(ValueError):
    """文案是自己代码拼的固定话术，可以透给用户。
    模型返回内容校验失败的 ValueError 会把模型的内容带进异常文本，不能透"""


class _NoRedirects(request.HTTPRedirectHandler):
    # 30x 一律不跟：标准库会抛 HTTPError，默认处理器则会自己去请求 Location 里的新地址。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_pinned_socket(addresses: tuple[str, ...], port: int, timeout, source_address):
    last_error = None
    for address in addresses:
        try:
            return socket.create_connection(
                (address, port), timeout, source_address,
            )
        except OSError as error:
            last_error = error
    if last_error is None:
        raise OSError("AI 草稿端点没有可连接的公网地址")
    raise last_error


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, host: str, addresses: tuple[str, ...], **kwargs):
        self._addresses = addresses
        super().__init__(host, **kwargs)

    def connect(self):
        raw_socket = _open_pinned_socket(
            self._addresses,
            self.port,
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(
            raw_socket,
            server_hostname=self.host,
        )


class _PinnedHTTPSHandler(request.HTTPSHandler):
    def __init__(self, addresses: tuple[str, ...]):
        self._addresses = addresses
        super().__init__(context=ssl.create_default_context())

    def https_open(self, req):
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPSConnection(
                host, self._addresses, **kwargs,
            ),
            req,
        )


def _build_request_opener(addresses: tuple[str, ...]) -> request.OpenerDirector:
    # 单独包一层，测试会把它整个换成假网络。
    return request.build_opener(
        request.ProxyHandler({}),
        _NoRedirects(),
        _PinnedHTTPSHandler(addresses),
    )


def _request_failure_message(error: Exception) -> str:
    if not isinstance(error, HTTPError):
        return "AI 草稿请求失败：无法连接 AI 服务"
    code = error.code
    reasons = {
        400: "请求参数或模型名不被服务接受",
        401: "API Key 未通过验证",
        403: "API Key 没有调用权限",
        404: "endpoint 路径或模型不存在",
        405: "endpoint 不支持当前请求方式",
        408: "服务端等待请求超时",
        429: "请求太频繁或额度不足",
    }
    if 300 <= code < 400:
        reason = "endpoint 地址要求重定向"
    elif 500 <= code < 600:
        reason = "服务端暂时不可用"
    else:
        reason = reasons.get(code, "请求被服务端拒绝")
    return f"AI 服务返回 HTTP {code}：{reason}"


def _is_private_host(host: str) -> bool:
    return is_private_host(host, allow_proxy_fake_ip=True)


def _catalog_prompt(schema: Dict[str, Any]) -> str:
    triggers = schema.get("triggers") if isinstance(schema, dict) else None
    actions = schema.get("actions") if isinstance(schema, dict) else None
    lines = [
        "你是 NotmyFault 规则草稿助手。",
        "先读下面的插件目录，挑出能满足用户需求的能力。",
        "目录里已有能满足需求的触发器和动作时调 propose_rule_draft。",
        "目录缺少能力时直接说明缺少的触发器或动作，并提示用户检查第三方插件。",
        "不要给出未经验证的插件名称、插件源码或安装操作。",
        "需要澄清、解释或追问时直接用普通文字回复，不要调工具，让用户看到流式输出。",
        "一次只调一个工具。触发器 id、动作 id 和参数名只能用目录里的。",
        "用户没给的值不要编；有 default 的参数可以用 default，否则省略。",
        "动作的 outputs 可以给后续动作的参数提供数据来源，起草多步规则时优先串联已有输出。",
        "可用触发器:",
    ]
    if isinstance(triggers, dict):
        for trigger_id, meta in triggers.items():
            trigger_name = meta.get("name", trigger_id) if isinstance(meta, dict) else trigger_id
            description = meta.get("description", "") if isinstance(meta, dict) else ""
            params = meta.get("params", []) if isinstance(meta, dict) else []
            lines.append(
                f"- {trigger_id}: {trigger_name}; {description}; "
                f"params={json.dumps(params, ensure_ascii=False, separators=(',', ':'))}"
            )
    lines.append("可用动作:")
    if isinstance(actions, dict):
        for action_id, meta in actions.items():
            action_name = meta.get("name", action_id) if isinstance(meta, dict) else action_id
            description = meta.get("description", "") if isinstance(meta, dict) else ""
            params = meta.get("params", []) if isinstance(meta, dict) else []
            extras = []
            if isinstance(meta, dict):
                outputs = meta.get("outputs") or []
                if isinstance(outputs, list) and outputs:
                    extras.append(
                        "outputs="
                        + json.dumps(outputs, ensure_ascii=False, separators=(',', ':'))
                    )
                permissions = meta.get("permissions") or []
                if isinstance(permissions, list) and permissions:
                    extras.append(
                        "permissions=" + json.dumps(permissions, ensure_ascii=False)
                    )
            suffix = ("; " + "; ".join(extras)) if extras else ""
            lines.append(
                f"- {action_id}: {action_name}; {description}; "
                f"params={json.dumps(params, ensure_ascii=False, separators=(',', ':'))}"
                f"{suffix}"
            )
    return "\n".join(lines)


def _system_prompt(schema: Dict[str, Any]) -> str:
    return _catalog_prompt(schema)


class _StreamDone:
    pass


_STREAM_DONE = _StreamDone()


class _SSEDecoder:
    """把上游 SSE 字节流切成事件，跨块和跨帧的边界都自己接上。"""

    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._data_lines: list[str] = []

    def feed(self, chunk: bytes) -> list[Any]:
        self._buffer += self._decoder.decode(chunk)
        events: list[Any] = []
        while True:
            index = self._buffer.find("\n")
            if index < 0:
                break
            line = self._buffer[:index]
            self._buffer = self._buffer[index + 1:]
            if line.endswith("\r"):
                line = line[:-1]
            if line == "":
                events.extend(self._drain_event())
                continue
            if line.startswith(":"):
                continue
            self._data_lines.append(line)
        return events

    def finish(self) -> list[Any]:
        self._buffer += self._decoder.decode(b"", final=True)
        if self._buffer:
            line = self._buffer.rstrip("\r\n")
            self._buffer = ""
            if line and not line.startswith(":"):
                self._data_lines.append(line)
        return self._drain_event()

    def _drain_event(self) -> list[Any]:
        data: list[str] = []
        for line in self._data_lines:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
        self._data_lines = []
        if not data:
            return []
        payload = "\n".join(data)
        if payload == "[DONE]":
            return [_STREAM_DONE]
        try:
            parsed = json.loads(payload)
        except ValueError:
            return []
        if not isinstance(parsed, dict):
            return []
        return [parsed]


class _ChatStreamAccumulator:
    """把 Chat 增量累积成完整的 Chat 返回体。"""

    def __init__(self) -> None:
        self._text = ""
        self._tool_calls: dict[int, dict] = {}

    def feed(self, event: dict) -> list[tuple[str, Any]]:
        deltas: list[tuple[str, Any]] = []
        choices = event.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        delta = first.get("delta") if isinstance(first, dict) else None
        if not isinstance(delta, dict):
            return deltas
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            deltas.append(("reasoning", reasoning))
        content = delta.get("content")
        if isinstance(content, str) and content:
            self._text += content
            deltas.append(("text", content))
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            saw_tool_delta = False
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                index = call.get("index")
                if not isinstance(index, int):
                    continue
                slot = self._tool_calls.setdefault(index, {"name": "", "arguments": ""})
                function = call.get("function")
                if isinstance(function, dict):
                    name = function.get("name")
                    if isinstance(name, str):
                        slot["name"] += name
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        slot["arguments"] += arguments
                    saw_tool_delta = True
            if saw_tool_delta:
                received = sum(len(item["arguments"]) for item in self._tool_calls.values())
                deltas.append(("progress", {
                    "phase": "drafting",
                    "received": received,
                }))
        return deltas

    def body(self) -> dict:
        message: dict = {"role": "assistant", "content": self._text}
        if self._tool_calls:
            message["tool_calls"] = [
                {"function": {"name": slot["name"], "arguments": slot["arguments"]}}
                for _index, slot in sorted(self._tool_calls.items())
            ]
        return {"choices": [{"message": message}]}


class _ResponsesStreamAccumulator:
    """把 Responses 增量累积成完整的 Responses 返回体。"""

    def __init__(self) -> None:
        self._text = ""
        self._call: dict = {"name": "", "arguments": "", "call_id": None}
        self._completed: dict | None = None

    def feed(self, event: dict) -> list[tuple[str, Any]]:
        deltas: list[tuple[str, Any]] = []
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self._text += delta
                deltas.append(("text", delta))
        elif event_type in (
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
        ):
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                deltas.append(("reasoning", delta))
        elif event_type == "response.function_call_arguments.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                self._call["arguments"] += delta
                deltas.append(("progress", {
                    "phase": "drafting",
                    "received": len(self._call["arguments"]),
                }))
        elif event_type == "response.function_call_arguments.done":
            arguments = event.get("arguments")
            if isinstance(arguments, str):
                self._call["arguments"] = arguments
        elif event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                name = item.get("name")
                if isinstance(name, str):
                    self._call["name"] = name
                call_id = item.get("call_id")
                if isinstance(call_id, str):
                    self._call["call_id"] = call_id
                arguments = item.get("arguments")
                if isinstance(arguments, str):
                    self._call["arguments"] = arguments
                deltas.append(("progress", {
                    "phase": "assembled",
                    "received": len(self._call["arguments"]),
                }))
        elif event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                deltas.append(("progress", {
                    "phase": "drafting",
                    "received": len(self._call["arguments"]),
                }))
        elif event_type == "response.completed":
            response = event.get("response")
            if isinstance(response, dict):
                self._completed = response
        return deltas

    def body(self) -> dict:
        if self._completed is not None:
            return self._completed
        output: list[dict] = []
        if self._text:
            output.append({"type": "message", "content": [
                {"type": "output_text", "text": self._text},
            ]})
        if self._call["name"] or self._call["arguments"]:
            output.append({
                "type": "function_call",
                "name": self._call["name"],
                "arguments": self._call["arguments"],
                "call_id": self._call["call_id"],
            })
        body: dict = {"output": output}
        if self._text:
            body["output_text"] = self._text
        return body


class OpenAICompatibleDraftProvider:
    def __init__(
        self,
        endpoint_url: str,
        model: str,
        api_key: str,
        api_format: str = "chat_completions",
    ):
        if not isinstance(endpoint_url, str):
            raise AIProviderRequestError("AI 草稿端点必须是字符串")
        parsed = parse.urlparse(endpoint_url)
        if parsed.scheme != "https":
            raise AIProviderRequestError("AI 草稿端点必须是 HTTPS")
        try:
            _parsed, addresses = resolve_public_http_url(
                endpoint_url,
                https_only=True,
                allow_proxy_fake_ip=True,
            )
        except ValueError as error:
            if "本机、内网" in str(error):
                raise AIProviderRequestError("AI 草稿端点不能指向内网地址") from None
            raise AIProviderRequestError("AI 草稿端点格式无效") from None
        if api_format not in {"chat_completions", "responses"}:
            raise AIProviderRequestError("AI 草稿接口格式无效")
        endpoint_url = endpoint_url.rstrip("/")
        suffix = "/responses" if api_format == "responses" else "/chat/completions"
        self._request_url = (
            endpoint_url if endpoint_url.endswith(suffix) else endpoint_url + suffix
        )
        self._api_format = api_format
        self._model = model
        self._api_key = api_key
        self._request_host = parsed.hostname or ""
        self._opener = _build_request_opener(addresses)
        self._response_lock = threading.Lock()
        self._stream_response = None
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._response_lock:
            response = self._stream_response
        if response is None:
            return
        raw = getattr(getattr(response, "fp", None), "raw", None)
        connection = getattr(raw, "_sock", None)
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        response.close()

    def __call__(
        self,
        messages: list[dict],
        schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        skill = build_rule_drafting_skill(schema)
        full_messages = [
            {"role": "system", "content": _system_prompt(schema)},
            *messages,
        ]
        payload: Dict[str, Any] = {"model": self._model, "tool_choice": "auto"}
        if self._api_format == "responses":
            payload["input"] = full_messages
            payload["tools"] = responses_tool_definitions(skill)
        else:
            payload["messages"] = full_messages
            payload["tools"] = chat_tool_definitions(skill)
        body = self._post(payload)
        if self._api_format == "responses":
            call, fallback = _extract_call_or_text(
                body, skill, extract_responses_tool_call, extract_responses_text
            )
        else:
            call, fallback = _extract_call_or_text(
                body, skill, extract_chat_tool_call, extract_chat_text
            )
        if call is None:
            return {"ok": True, "result_type": "assistant_message", "message": fallback}
        return _dispatch_tool_call(call, schema)

    def _build_request(self, payload: Dict[str, Any], *, stream: bool = False) -> request.Request:
        if _is_private_host(self._request_host):
            raise AIProviderRequestError("AI 草稿端点不能指向内网地址")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-type": "application/json",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        return request.Request(
            self._request_url,
            data=data,
            headers=headers,
            method="POST",
        )

    def _post(self, payload: Dict[str, Any]) -> Any:
        req = self._build_request(payload)
        try:
            # HTTPError、socket、SSL 报错都在 OSError 下；HTTPException 是读响应中途断线的报错。
            with self._opener.open(req, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except (OSError, HTTPException) as error:
            raise AIProviderRequestError(_request_failure_message(error)) from error
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise AIProviderRequestError("AI 返回内容过大")
        try:
            return json.loads(raw)
        except ValueError:
            raise AIProviderRequestError("AI 返回的不是有效 JSON") from None

    def stream(
        self,
        messages: list[dict],
        schema: Dict[str, Any],
        *,
        idle_timeout: float = _IDLE_TIMEOUT_SECONDS,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[tuple[str, Any]]:
        """流式调用，产出 (kind, payload)；kind 是 reasoning/text/result。"""
        skill = build_rule_drafting_skill(schema)
        full_messages = [
            {"role": "system", "content": _system_prompt(schema)},
            *messages,
        ]
        payload: Dict[str, Any] = {
            "model": self._model,
            "tool_choice": "auto",
            "stream": True,
        }
        if self._api_format == "responses":
            payload["input"] = full_messages
            payload["tools"] = responses_tool_definitions(skill)
            accumulator = _ResponsesStreamAccumulator()
        else:
            payload["messages"] = full_messages
            payload["tools"] = chat_tool_definitions(skill)
            accumulator = _ChatStreamAccumulator()

        req = self._build_request(payload, stream=True)
        decoder = _SSEDecoder()
        total_bytes = 0
        finished = False
        if self._cancelled.is_set() or (should_stop is not None and should_stop()):
            return
        try:
            with self._opener.open(req, timeout=idle_timeout) as response:
                with self._response_lock:
                    self._stream_response = response
                while True:
                    if self._cancelled.is_set() or (should_stop is not None and should_stop()):
                        return
                    chunk = response.read(_STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > _MAX_RESPONSE_BYTES:
                        raise AIProviderRequestError("AI 返回内容过大")
                    for event in decoder.feed(chunk):
                        if event is _STREAM_DONE:
                            finished = True
                            break
                        for delta in accumulator.feed(event):
                            yield delta
                    if finished:
                        break
                if not finished:
                    for event in decoder.finish():
                        if event is _STREAM_DONE:
                            break
                        for delta in accumulator.feed(event):
                            yield delta
        except (socket.timeout, TimeoutError) as error:
            raise AIProviderIdleTimeoutError("AI 草稿请求超时") from error
        except (OSError, HTTPException) as error:
            raise AIProviderRequestError(_request_failure_message(error)) from error
        finally:
            with self._response_lock:
                self._stream_response = None

        body = accumulator.body()
        if self._api_format == "responses":
            call, fallback = _extract_call_or_text(
                body, skill, extract_responses_tool_call, extract_responses_text
            )
        else:
            call, fallback = _extract_call_or_text(
                body, skill, extract_chat_tool_call, extract_chat_text
            )
        if call is None:
            yield ("result", {
                "ok": True,
                "result_type": "assistant_message",
                "message": fallback,
            })
        else:
            yield ("progress", {"phase": "validating", "received": 0})
            yield ("result", _dispatch_tool_call(call, schema))


def _dispatch_tool_call(call: ToolCall, catalog: Dict[str, Any]) -> Dict[str, Any]:
    if call.name == "propose_rule_draft":
        return {
            "ok": True,
            "result_type": "rule_draft",
            "candidates": [parse_rule_draft(call.arguments, catalog)],
        }
    if call.name == "reply":
        message = _require_str(
            call.arguments.get("message"), "reply.message", "bad_arguments"
        )
        return {"ok": True, "result_type": "assistant_message", "message": message}
    raise ToolCallError("unknown_tool", f"技能不允许工具 {call.name}")


def _extract_call_or_text(
    body: Any,
    skill: SkillSpec,
    extractor: Callable[[Any, SkillSpec], ToolCall],
    text_extractor: Callable[[Any], str],
) -> tuple[ToolCall | None, str]:
    try:
        return extractor(body, skill), ""
    except ToolCallError as error:
        if error.code != "no_tool_call":
            raise
        text = text_extractor(body)
        if not text.strip():
            raise
        return None, text


def draft_from_openai_compatible(
    provider: OpenAICompatibleDraftProvider,
    messages: list[dict],
    schema: Dict[str, Any],
) -> Dict[str, Any]:
    return provider(messages, schema)
