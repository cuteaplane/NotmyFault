import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from notmyfault.host.api.services.ai_drafting import (
    AIDraftingError,
    AIDraftingService,
)


def _error_response(error: AIDraftingError) -> JSONResponse:
    return JSONResponse(error.body, status_code=error.status_code)


def create_ai_router(service: AIDraftingService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/settings/ai-drafting")
    async def ai_drafting_settings():
        return service.settings()

    @router.put("/api/settings/ai-drafting/api-key")
    async def save_ai_api_key(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "无效的 JSON 请求体"},
                status_code=400,
            )
        key = body.get("api_key") if isinstance(body, dict) else None
        try:
            return service.save_api_key(key)
        except AIDraftingError as error:
            return _error_response(error)

    @router.delete("/api/settings/ai-drafting/api-key")
    async def delete_ai_api_key():
        try:
            return service.delete_api_key()
        except AIDraftingError as error:
            return _error_response(error)

    @router.put("/api/settings/ai-drafting")
    async def update_ai_drafting_settings(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "无效的 JSON 请求体"},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "请求体必须是 JSON 对象"},
                status_code=400,
            )
        try:
            return service.update_settings(body)
        except AIDraftingError as error:
            return _error_response(error)

    @router.post("/api/rules/draft/ai")
    async def ai_rules_draft(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "无效的 JSON 请求体"},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "请求体必须是 JSON 对象"},
                status_code=400,
            )
        try:
            return await service.draft(body)
        except AIDraftingError as error:
            return _error_response(error)

    @router.post("/api/rules/draft/ai/stream")
    async def ai_rules_draft_stream(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "请求体必须是 JSON 对象"},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "请求体必须是 JSON 对象"},
                status_code=400,
            )
        try:
            plan = service.prepare_stream(body)
        except AIDraftingError as error:
            return _error_response(error)

        async def generate():
            async for event, payload in service.stream(
                plan,
                request.is_disconnected,
            ):
                yield f"event: {event}\n"
                yield "data: " + json.dumps(
                    payload,
                    ensure_ascii=False,
                ) + "\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
