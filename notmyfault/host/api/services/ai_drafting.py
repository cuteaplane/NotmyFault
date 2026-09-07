from __future__ import annotations

import asyncio
import os
import inspect
import threading
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List

from notmyfault.config import (
    ConfigValidationError,
    SignedConfigStore,
    ensure_rule_binding_ids,
    get_ai_drafting_settings,
)
from notmyfault.host.ai_provider import (
    AIProviderIdleTimeoutError,
    AIProviderRequestError,
    OpenAICompatibleDraftProvider,
    draft_from_openai_compatible,
)
from notmyfault.security.api_key_store import (
    KeyStoreError,
    KeyStoreInvalidKeyError,
    KeyStoreStatus,
    KeyStoreUnsupportedError,
)


_STATUS_LABELS = {
    KeyStoreStatus.STORED: "saved",
    KeyStoreStatus.ABSENT: "none",
    KeyStoreStatus.UNSUPPORTED: "unsupported",
    KeyStoreStatus.CORRUPT: "corrupt",
}
_MAX_MESSAGES = 40
_MAX_CONTENT_CHARS = 4000
_MAX_ASSISTANT_CHARS = 100_000


@dataclass(frozen=True, slots=True)
class AIDraftingError(Exception):
    status_code: int
    body: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class AIStreamPlan:
    messages: List[Dict[str, str]]
    schema: Dict[str, Any]
    body: Dict[str, Any]
    settings: Dict[str, Any]


class AIDraftingService:
    def __init__(
        self,
        store: SignedConfigStore,
        plugin_schema: Callable[[], Dict[str, Any]],
        validate_rule: Callable[[Any], Dict[str, Any]],
        key_store: Any,
        provider: Any = None,
    ) -> None:
        self._store = store
        self._stream_slots = threading.BoundedSemaphore(2)
        self._plugin_schema = plugin_schema
        self._validate_rule = validate_rule
        self._provider = provider
        self._key_store = key_store

    def settings(self) -> Dict[str, Any]:
        settings = get_ai_drafting_settings(self._load_config())
        settings["api_key_status"] = self._api_key_status_label()
        return settings

    def save_api_key(self, key: Any) -> Dict[str, Any]:
        if not isinstance(key, str) or not key.strip():
            self._fail(400, "API key 不能为空")
        try:
            self._key_store.save_api_key(key)
        except KeyStoreInvalidKeyError as error:
            raise AIDraftingError(
                400,
                {"ok": False, "error": "API key 无效"},
            ) from error
        except KeyStoreUnsupportedError as error:
            raise AIDraftingError(
                409,
                {"ok": False, "error": "当前平台不支持保存 API key"},
            ) from error
        except KeyStoreError as error:
            raise AIDraftingError(
                500,
                {"ok": False, "error": "无法保存 API key"},
            ) from error
        return {"ok": True, "api_key_status": self._api_key_status_label()}

    def delete_api_key(self) -> Dict[str, Any]:
        try:
            self._key_store.delete_api_key()
        except KeyStoreError as error:
            raise AIDraftingError(
                500,
                {"ok": False, "error": "无法删除 API key"},
            ) from error
        return {"ok": True, "api_key_status": self._api_key_status_label()}

    def update_settings(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            config = self._load_config_for_update()
        except ConfigValidationError as error:
            raise AIDraftingError(
                409,
                {"ok": False, "error": "配置未通过完整性校验"},
            ) from error
        settings = config.get("settings")
        settings = dict(settings) if isinstance(settings, dict) else {}
        settings["ai_drafting"] = {
            "enabled": (
                body.get("enabled")
                if isinstance(body.get("enabled"), bool)
                else False
            ),
            "endpoint_url": (
                body.get("endpoint_url")
                if isinstance(body.get("endpoint_url"), str)
                else ""
            ),
            "model": (
                body.get("model")
                if isinstance(body.get("model"), str)
                else ""
            ),
            "api_format": (
                body.get("api_format")
                if body.get("api_format") in {"chat_completions", "responses"}
                else "chat_completions"
            ),
        }
        config["settings"] = settings
        if not self._store.save_config(config):
            self._fail(500, "无法保存配置")
        return {"ok": True, "settings": get_ai_drafting_settings(config)}

    async def draft(self, body: Dict[str, Any]) -> Dict[str, Any]:
        plan = self._prepare(body)
        try:
            if callable(self._provider):
                provider_result = await asyncio.to_thread(
                    self._provider,
                    plan.messages,
                    plan.schema,
                )
            else:
                provider_result = await asyncio.to_thread(
                    draft_from_openai_compatible,
                    self._build_configured_provider(plan.body, plan.settings),
                    plan.messages,
                    plan.schema,
                )
        except Exception as error:
            raise AIDraftingError(
                502,
                {
                    "ok": False,
                    "code": "ai_provider_failed",
                    "error": f"AI 服务返回错误：{self._error_detail(error)}",
                },
            ) from error
        try:
            return self._finalize(provider_result)
        except Exception as error:
            raise AIDraftingError(
                502,
                {
                    "ok": False,
                    "code": "ai_provider_failed",
                    "error": f"处理结果失败：{self._error_detail(error)}",
                },
            ) from error

    def prepare_stream(self, body: Dict[str, Any]) -> AIStreamPlan:
        return self._prepare(body)

    async def stream(
        self,
        plan: AIStreamPlan,
        is_disconnected: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[tuple[str, Dict[str, Any]]]:
        if not self._stream_slots.acquire(blocking=False):
            yield "error", {"code": "ai_busy", "error": "AI 请求仍在结束，请稍后重试"}
            yield "done", {"status": "done"}
            return
        items: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancel_event = threading.Event()
        active_provider = None

        def enqueue(item):
            if not cancel_event.is_set() and not loop.is_closed():
                loop.call_soon_threadsafe(items.put_nowait, item)

        def make_stream():
            nonlocal active_provider
            active_provider = self._provider
            if self._provider is not None:
                provider_stream = getattr(self._provider, "stream", None)
                if callable(provider_stream):
                    parameters = inspect.signature(provider_stream).parameters
                    options = {"should_stop": cancel_event.is_set} if "should_stop" in parameters else {}
                    return provider_stream(
                        plan.messages,
                        plan.schema,
                        **options,
                    )
                if callable(self._provider):
                    def fallback():
                        yield (
                            "result",
                            self._provider(
                                plan.messages,
                                plan.schema,
                            ),
                        )

                    return fallback()
            configured = self._build_configured_provider(
                plan.body,
                plan.settings,
            )
            active_provider = configured
            return configured.stream(
                plan.messages,
                plan.schema,
                should_stop=cancel_event.is_set,
                idle_timeout=120,
            )

        def run() -> None:
            failure: Exception | None = None
            generator = None
            try:
                generator = make_stream()
                for kind, payload in generator:
                    if cancel_event.is_set():
                        break
                    enqueue(("event", kind, payload))
            except Exception as error:
                failure = error
            finally:
                try:
                    if generator is not None:
                        generator.close()
                finally:
                    self._stream_slots.release()
            if not cancel_event.is_set():
                marker = (
                    ("error", failure)
                    if failure is not None
                    else ("finish", None)
                )
                enqueue(marker)

        worker = threading.Thread(
            target=run,
            daemon=True,
            name="ai-draft-stream",
        )
        worker.start()
        finalized = False
        disconnected = False
        try:
            yield "status", {"status": "started"}
            while True:
                if await is_disconnected():
                    disconnected = True
                    break
                try:
                    item = await asyncio.wait_for(items.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    if not worker.is_alive():
                        break
                    continue
                tag = item[0]
                if tag == "event":
                    _tag, kind, payload = item
                    if finalized:
                        continue
                    if kind in ("reasoning", "text"):
                        yield kind, {"delta": payload}
                    elif kind == "progress":
                        yield "progress", payload
                    elif kind == "result":
                        finalized = True
                        try:
                            result = self._finalize(payload)
                        except Exception as error:
                            yield "error", {
                                "code": "ai_provider_failed",
                                "error": (
                                    "处理结果失败："
                                    + self._error_detail(error)
                                ),
                            }
                        else:
                            yield "result", result
                elif tag == "error":
                    if not finalized:
                        finalized = True
                        failure = item[1]
                        if isinstance(failure, AIProviderIdleTimeoutError):
                            payload = {
                                "code": "idle_timeout",
                                "error": "AI 服务超过 120 秒没有返回内容，可能是模型响应太慢或网络问题",
                            }
                        else:
                            payload = {
                                "code": "ai_provider_failed",
                                "error": (
                                    "AI 服务返回错误："
                                    + self._error_detail(failure)
                                ),
                            }
                        yield "error", payload
                    break
                elif tag == "finish":
                    break
        finally:
            cancel_event.set()
            cancel = getattr(active_provider, "cancel", None)
            if callable(cancel):
                await asyncio.to_thread(cancel)
            await asyncio.to_thread(worker.join, 1.0)
        if not disconnected:
            yield "done", {"status": "done"}

    def _prepare(self, body: Dict[str, Any]) -> AIStreamPlan:
        settings = get_ai_drafting_settings(self._load_config())
        if not settings["enabled"]:
            raise AIDraftingError(
                403,
                {"ok": False, "code": "ai_disabled"},
            )
        messages, message_error = _validate_messages(body.get("messages"))
        if message_error is not None or messages is None:
            self._fail(400, message_error or "messages 无效")
        if "consent" in body:
            self._fail(400, "consent 已停用")
        return AIStreamPlan(
            messages=messages,
            schema=self._plugin_schema(),
            body=body,
            settings=settings,
        )

    def _build_configured_provider(
        self,
        body: Dict[str, Any],
        settings: Dict[str, Any],
    ) -> OpenAICompatibleDraftProvider:
        endpoint_url = body.get("endpoint_url") or settings.get("endpoint_url")
        model = body.get("model") or settings.get("model")
        api_key = body.get("api_key")
        if not api_key and endpoint_url == settings.get("endpoint_url"):
            api_key = self._load_saved_api_key()
        if not endpoint_url or not model or not api_key:
            raise AIProviderRequestError(
                "AI 服务未配置：请先在设置中填写端点地址、模型名称并保存 API Key"
            )
        return OpenAICompatibleDraftProvider(
            endpoint_url=endpoint_url,
            model=model,
            api_key=api_key,
            api_format=settings.get("api_format", "chat_completions"),
        )

    def _finalize(
        self,
        provider_result: Any,
    ) -> Dict[str, Any]:
        if not isinstance(provider_result, dict):
            raise ValueError("invalid AI draft result")
        result_type = provider_result.get("result_type", "rule_draft")
        if result_type == "assistant_message":
            message = provider_result.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("invalid AI assistant message")
            if len(message) > _MAX_ASSISTANT_CHARS:
                raise AIProviderRequestError("AI 回复内容过大")
            return {
                "ok": True,
                "source": "ai",
                "result_type": "assistant_message",
                "message": message,
            }
        if result_type != "rule_draft":
            raise ValueError("unknown AI draft result type")
        candidates = provider_result.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 1:
            raise ValueError("invalid AI draft candidates")
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ValueError("invalid AI draft candidate")
        draft = dict(candidate)
        if "event" not in draft and isinstance(draft.get("trigger"), dict):
            draft["event"] = draft.pop("trigger")
        draft = ensure_rule_binding_ids(draft)
        return {
            "ok": True,
            "source": "ai",
            "result_type": "rule_draft",
            "draft": draft,
            "validation": self._validate_rule(draft),
        }

    def _api_key_status_label(self) -> str:
        try:
            status = self._key_store.api_key_status()
        except KeyStoreError:
            return "corrupt"
        return _STATUS_LABELS.get(status, "corrupt")

    def _load_saved_api_key(self) -> str | None:
        try:
            return self._key_store.load_api_key()
        except KeyStoreError:
            return None

    def _load_config(self) -> Dict[str, Any]:
        try:
            return self._store.load_verified_config()
        except ConfigValidationError:
            return {"rules": []}

    def _load_config_for_update(self) -> Dict[str, Any]:
        if not os.path.exists(self._store.config_path):
            if not self._store.save_config({}):
                raise ConfigValidationError("无法创建配置文件")
        return self._store.load_verified_config()

    @staticmethod
    def _error_detail(error: Exception | None) -> str:
        if isinstance(error, AIProviderRequestError):
            return str(error)
        return "AI 草稿服务暂不可用"

    @staticmethod
    def _fail(status_code: int, message: str) -> None:
        raise AIDraftingError(
            status_code,
            {"ok": False, "error": message},
        )


def _validate_messages(
    value: Any,
) -> tuple[List[Dict[str, str]] | None, str | None]:
    if not isinstance(value, list) or not value:
        return None, "messages 必须是非空列表"
    if len(value) > _MAX_MESSAGES:
        return None, f"messages 最多 {_MAX_MESSAGES} 条"
    normalized = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return None, f"messages[{index}] 必须是对象"
        if set(item.keys()) != {"role", "content"}:
            return None, f"messages[{index}] 只能包含 role 和 content"
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant"):
            return None, f"messages[{index}].role 必须是 user 或 assistant"
        if not isinstance(content, str) or not content.strip():
            return None, f"messages[{index}].content 必须是非空字符串"
        if len(content) > _MAX_CONTENT_CHARS:
            return None, f"messages[{index}].content 超过 {_MAX_CONTENT_CHARS} 字符"
        normalized.append({"role": role, "content": content})
    if normalized[-1]["role"] != "user":
        return None, "最后一条消息必须是 user 角色"
    return normalized, None
