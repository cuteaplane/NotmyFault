from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from typing import Any, Dict

from notmyfault.extensions.protocol import (
    OwnedValueError,
    owned_value_identity,
    unpack_owned_value,
    value_matches_type,
)
from notmyfault.extensions.session import ExtensionContext, ExtensionSessionManager
from notmyfault.host.api.ports import EngineControlPort
from notmyfault.core.data_types import DataTypeError, normalize_value
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.value_codec import encode_value


_MESSAGE_MAX_BYTES = 1024 * 1024
_MAX_CONCURRENT_INVOCATIONS = 8


@dataclass(frozen=True, slots=True)
class PluginInteractionError(Exception):
    status_code: int
    body: Dict[str, Any]


class PluginInteractionService:
    def __init__(
        self,
        engine: EngineControlPort,
        extension_sessions: ExtensionSessionManager,
    ) -> None:
        self._engine = engine
        self._extension_sessions = extension_sessions
        self._invoke_slots = threading.BoundedSemaphore(
            _MAX_CONCURRENT_INVOCATIONS
        )

    def extensions(self) -> Dict[str, Any]:
        engine = self._engine.current_engine
        if engine is None:
            return {
                "commands": [],
                "parameter_editors": [],
                "views": [],
                "data_types": [],
            }
        return engine.extensions.public_contributions()

    async def invoke_extension(
        self,
        plugin_id: str,
        command_id: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        engine = self._engine.current_engine
        if engine is None:
            self._fail(409, "自动化引擎未运行，无法调用插件扩展")
        registry = TypeRegistry.from_plugins(engine.triggers_meta, engine.actions_meta)
        session_id = body.get("session_id")
        if isinstance(session_id, str) and session_id:
            session = self._extension_sessions.get(session_id)
            if session is None:
                self._fail(404, "扩展会话不存在或已过期")
            if session.plugin_id != plugin_id:
                self._fail(400, "扩展会话不属于该插件")
            current_plugin = engine.extensions.plugin(plugin_id)
            if (
                current_plugin is None
                or current_plugin.get("meta") is not session.plugin_meta
            ):
                self._extension_sessions.drop(session.session_id)
                self._fail(409, "插件已经重新加载，请重新打开编辑器")
            if command_id not in session.allowed_commands:
                self._fail(403, "当前视图不能调用这个命令")
        else:
            source_kind = body.get("source_kind")
            source_id = body.get("source_id")
            if not isinstance(source_kind, str) or not isinstance(source_id, str):
                self._fail(400, "缺少扩展入口信息")
            allowed_commands = engine.extensions.source_commands(
                plugin_id,
                source_kind,
                source_id,
            )
            if allowed_commands is None:
                self._fail(404, "扩展入口不存在")
            if command_id not in allowed_commands:
                self._fail(403, "该入口没有声明这个命令")
            source = engine.extensions.contribution(
                plugin_id,
                source_kind,
                source_id,
            )
            data_type_id = source.get("data_type", "") if source else ""
            data_type = engine.extensions.data_type(plugin_id, data_type_id)
            plugin = engine.extensions.plugin(plugin_id)
            value_type = source.get("value_type") if source else None
            if source is None or plugin is None:
                self._fail(400, "扩展入口的数据类型不可用")
            if data_type is None and not isinstance(value_type, (str, dict)):
                self._fail(400, "扩展入口的数据类型不可用")
            current_value = body.get("current_value")
            if current_value is not None and data_type is not None:
                try:
                    if owned_value_identity(current_value) is not None:
                        current_value = unpack_owned_value(
                            current_value,
                            plugin["meta"]["package_name"],
                            data_type["id"],
                            data_type["version"],
                        )
                    elif isinstance(current_value, dict) and "$type" in current_value:
                        raise OwnedValueError("插件数据的归属信息无效")
                    elif source.get("accepts_legacy") is not True:
                        raise OwnedValueError("当前值不是该插件声明的数据")
                except OwnedValueError as error:
                    self._fail(400, str(error))
            elif current_value is not None and not value_matches_type(
                current_value, value_type, registry
            ):
                self._fail(400, f"当前值不是 {value_type}")
            session = self._extension_sessions.create(
                plugin_id=plugin_id,
                command_id=command_id,
                plugin_meta=plugin["meta"],
                source_kind=source_kind,
                source_id=source_id,
                allowed_commands=allowed_commands,
                data_type=data_type,
                current_value=current_value,
                value_type=value_type if data_type is None else None,
            )
            session.type_registry = registry

        handler = engine.extension_handler(plugin_id, command_id)
        if handler is None:
            self._extension_sessions.drop(session.session_id)
            self._fail(404, f"插件命令不可用: {command_id}")
        context = ExtensionContext(session, engine.extensions)

        def invoke_handler() -> Any:
            if not self._invoke_slots.acquire(blocking=False):
                raise RuntimeError("扩展命令并发数已达上限")
            try:
                return session.invoke(handler, context, body.get("payload"))
            finally:
                self._invoke_slots.release()

        try:
            result = await asyncio.to_thread(invoke_handler)
        except Exception as error:
            raise PluginInteractionError(
                400,
                {
                    "ok": False,
                    "error": "插件命令调用失败",
                    "session_id": session.session_id,
                },
            ) from error
        if result is None:
            result = {"ok": True}
        if not isinstance(result, dict):
            result = {"ok": True, "data": result}
        if result.get("ok") is False:
            if result.get("close") is True:
                self._extension_sessions.drop(session.session_id)
            raise PluginInteractionError(
                400,
                {
                    "ok": False,
                    "error": result.get("error", "插件命令调用失败"),
                    "session_id": session.session_id,
                },
            )
        if "value" in result:
            if session.data_type is None:
                if not value_matches_type(
                    result["value"], session.value_type or "", registry
                ):
                    self._extension_sessions.drop(session.session_id)
                    self._fail(400, f"插件命令返回值不是 {session.value_type}")
            else:
                try:
                    if session.data_type.get("binding") == "shared":
                        identity = f"{session.plugin_meta['package_name']}/{session.data_type['id']}@{session.data_type['version']}"
                        result["value"] = normalize_value(result["value"], identity, registry)
                    unpack_owned_value(
                        result["value"],
                        session.plugin_meta["package_name"],
                        session.data_type["id"],
                        session.data_type["version"],
                    )
                except (OwnedValueError, DataTypeError) as error:
                    self._extension_sessions.drop(session.session_id)
                    self._fail(400, str(error))
        response = {
            "ok": True,
            "session_id": session.session_id,
            "data": result.get("data"),
            "close": result.get("close") is True,
        }
        for key in ("view", "state", "value"):
            if key in result:
                response[key] = result[key]
        if session.status:
            response["status"] = session.status
        try:
            response_size = len(
                json.dumps(encode_value(response), ensure_ascii=False).encode("utf-8")
            )
        except (TypeError, ValueError) as error:
            raise PluginInteractionError(
                400,
                {"ok": False, "error": "插件命令返回了无法保存为 JSON 的数据"},
            ) from error
        if response_size > _MESSAGE_MAX_BYTES:
            raise PluginInteractionError(
                413,
                {"ok": False, "error": "插件命令返回数据不能超过 1 MiB"},
            )
        if response["close"]:
            self._extension_sessions.drop(session.session_id)
        return response

    def close_extension_session(
        self,
        plugin_id: str,
        session_id: str,
    ) -> Dict[str, Any]:
        session = self._extension_sessions.get(session_id)
        if session is None:
            return {"ok": True}
        if session.plugin_id != plugin_id:
            self._fail(400, "扩展会话不属于该插件")
        self._extension_sessions.drop(session_id)
        return {"ok": True}

    def view_page(self, plugin_id: str, view_id: str) -> Dict[str, Any]:
        engine = self._engine.current_engine
        if engine is None:
            self._fail(409, "自动化引擎未运行，无法读取插件视图")
        page = engine.extensions.view_page_path(plugin_id, view_id)
        if page is None:
            self._fail(404, "插件视图不存在")
        try:
            with open(page, "rb") as file:
                raw_html = file.read(_MESSAGE_MAX_BYTES + 1)
            if len(raw_html) > _MESSAGE_MAX_BYTES:
                self._fail(413, "插件视图不能超过 1 MiB")
            html = raw_html.decode("utf-8")
        except PluginInteractionError:
            raise
        except (OSError, UnicodeDecodeError) as error:
            raise PluginInteractionError(
                500,
                {"ok": False, "error": "无法读取插件视图"},
            ) from error
        return {"ok": True, "html": html}

    @staticmethod
    def _fail(status_code: int, message: str) -> None:
        raise PluginInteractionError(
            status_code,
            {"ok": False, "error": message},
        )
