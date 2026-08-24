from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict

from notmyfault.components.session import ComponentSessionManager
from notmyfault.extensions.protocol import (
    OwnedValueError,
    owned_value_identity,
    unpack_owned_value,
)
from notmyfault.extensions.session import ExtensionContext, ExtensionSessionManager
from notmyfault.host.api.ports import EngineControlPort


_MESSAGE_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PluginInteractionError(Exception):
    status_code: int
    body: Dict[str, Any]


class PluginInteractionService:
    def __init__(
        self,
        engine: EngineControlPort,
        component_sessions: ComponentSessionManager,
        extension_sessions: ExtensionSessionManager,
    ) -> None:
        self._engine = engine
        self._component_sessions = component_sessions
        self._extension_sessions = extension_sessions

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
            data_type = engine.extensions.data_type(
                plugin_id,
                source.get("data_type", "") if source else "",
            )
            plugin = engine.extensions.plugin(plugin_id)
            if source is None or data_type is None or plugin is None:
                self._fail(400, "扩展入口的数据类型不可用")
            current_value = body.get("current_value")
            if current_value is not None:
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
            session = self._extension_sessions.create(
                plugin_id=plugin_id,
                command_id=command_id,
                plugin_meta=plugin["meta"],
                source_kind=source_kind,
                source_id=source_id,
                allowed_commands=allowed_commands,
                data_type=data_type,
                current_value=current_value,
            )

        handler = engine.extension_handler(plugin_id, command_id)
        if handler is None:
            self._extension_sessions.drop(session.session_id)
            self._fail(404, f"插件命令不可用: {command_id}")
        context = ExtensionContext(session, engine.extensions)
        try:
            result = await asyncio.to_thread(
                session.invoke,
                handler,
                context,
                body.get("payload"),
            )
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
            raise PluginInteractionError(
                400,
                {
                    "ok": False,
                    "error": result.get("error", "插件命令调用失败"),
                    "session_id": session.session_id,
                },
            )
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
                json.dumps(response, ensure_ascii=False).encode("utf-8")
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

    def components(self) -> Dict[str, Any]:
        engine = self._engine.current_engine
        if engine is None:
            return {"components": []}
        items = []
        for kind, meta_store in (
            ("actions", engine.actions_meta),
            ("triggers", engine.triggers_meta),
        ):
            for plugin_id, meta in sorted(meta_store.items()):
                components = meta.get("components")
                if not isinstance(components, list):
                    continue
                for component in components:
                    if not isinstance(component, dict):
                        continue
                    component_id = component.get("id", "")
                    items.append(
                        {
                            "plugin_id": plugin_id,
                            "kind": kind,
                            "id": component_id,
                            "name": component.get("name", component_id),
                            "description": component.get("description", ""),
                            "api": component.get("api", "component-v1"),
                            "ui": component.get("ui", {}),
                            "available": engine.component(
                                plugin_id,
                                component_id,
                            )
                            is not None,
                        }
                    )
        return {"components": items}

    async def invoke_component(
        self,
        plugin_id: str,
        component_id: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        engine = self._engine.current_engine
        if engine is None:
            self._fail(404, "自动化引擎未运行，无法调用插件组件")
        module = engine.component(plugin_id, component_id)
        if module is None:
            self._fail(404, f"插件组件不可用: {plugin_id}/{component_id}")
        method = body.get("method", "")
        if not isinstance(method, str) or not method:
            self._fail(400, "缺少 method 字段")
        payload = body.get("payload")
        session_id = body.get("session_id")
        meta = (
            engine.actions_meta.get(plugin_id)
            or engine.triggers_meta.get(plugin_id)
            or {}
        )
        if isinstance(session_id, str) and session_id:
            session = self._component_sessions.get(session_id)
            if session is None:
                self._fail(404, "组件会话不存在或已过期")
            if (
                session.plugin_id != plugin_id
                or session.component_id != component_id
            ):
                self._fail(400, "会话不属于该组件")
        else:
            session = self._component_sessions.create(plugin_id, component_id, meta)
        try:
            result = await asyncio.to_thread(
                module.invoke,
                session,
                method,
                payload,
            )
        except Exception as error:
            self._component_sessions.drop(session.session_id)
            raise PluginInteractionError(
                400,
                {"ok": False, "error": "组件调用失败"},
            ) from error
        if result is None:
            result = {}
        if not isinstance(result, dict):
            result = {"data": result}
        if result.get("ok") is False:
            self._component_sessions.drop(session.session_id)
            self._fail(400, result.get("error", "组件调用失败"))
        if result.get("close") is True:
            self._component_sessions.drop(session.session_id)
        response = {
            "ok": True,
            "session_id": session.session_id,
            "data": result,
        }
        if session.status:
            response["status"] = session.status
        return response

    @staticmethod
    def _fail(status_code: int, message: str) -> None:
        raise PluginInteractionError(
            status_code,
            {"ok": False, "error": message},
        )
