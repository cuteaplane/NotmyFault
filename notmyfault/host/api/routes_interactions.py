import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from notmyfault.host.api.value_transport import decode_request, value_response

from notmyfault.host.api.services.interactions import (
    PluginInteractionError,
    PluginInteractionService,
)


_MESSAGE_MAX_BYTES = 1024 * 1024


def _error_response(error: PluginInteractionError) -> JSONResponse:
    return JSONResponse(error.body, status_code=error.status_code)


def create_interactions_router(service: PluginInteractionService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/plugins/extensions")
    async def plugins_extensions_list():
        return service.extensions()

    @router.post(
        "/api/plugins/{plugin_id}/extensions/commands/{command_id}/invoke"
    )
    async def plugin_extension_command_invoke(
        plugin_id: str,
        command_id: str,
        request: Request,
    ):
        raw_body = await request.body()
        if len(raw_body) > _MESSAGE_MAX_BYTES:
            return JSONResponse(
                {"ok": False, "error": "扩展请求不能超过 1 MiB"},
                status_code=413,
            )
        try:
            body = decode_request(request, json.loads(raw_body) if raw_body else {})
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "扩展请求数据编码无效"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "扩展请求必须是 JSON 对象"},
                status_code=400,
            )
        try:
            return value_response(await service.invoke_extension(plugin_id, command_id, body))
        except PluginInteractionError as error:
            return _error_response(error)

    @router.delete(
        "/api/plugins/{plugin_id}/extensions/sessions/{session_id}"
    )
    async def plugin_extension_session_close(plugin_id: str, session_id: str):
        try:
            return service.close_extension_session(plugin_id, session_id)
        except PluginInteractionError as error:
            return _error_response(error)

    @router.get("/api/plugins/{plugin_id}/extensions/views/{view_id}/page")
    async def plugin_extension_view_page(plugin_id: str, view_id: str):
        try:
            return service.view_page(plugin_id, view_id)
        except PluginInteractionError as error:
            return _error_response(error)

    return router
