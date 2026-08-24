import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from notmyfault.host.api.events import EventBroker
from notmyfault.host.api.services.engine import EngineService, EngineServiceError


def _service_error(error: EngineServiceError) -> JSONResponse:
    status_code = {
        "invalid": 400,
        "conflict": 409,
        "not_found": 404,
    }.get(error.kind, 500)
    return JSONResponse(error.body, status_code=status_code)


def create_engine_router(
    service: EngineService,
    events: EventBroker,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/engine/start")
    async def engine_start():
        try:
            return service.start()
        except EngineServiceError as error:
            return _service_error(error)

    @router.post("/api/engine/stop")
    async def engine_stop():
        return service.stop()

    @router.post("/api/engine/shutdown")
    async def engine_shutdown():
        return service.shutdown()

    @router.get("/api/engine/status")
    async def engine_status():
        return service.status()

    @router.get("/api/platform")
    async def platform_status():
        return service.platform_status()

    @router.get("/api/engine/diagnostics")
    async def engine_diagnostics():
        return service.diagnostics()

    @router.get("/api/runs")
    async def runs_list(limit: int = 100):
        return service.runs(limit)

    @router.get("/api/runs/{run_id}")
    async def run_detail(run_id: str):
        run = service.run_detail(run_id)
        if run is None:
            return JSONResponse({"detail": "运行记录不存在"}, status_code=404)
        return run

    @router.post("/api/runs/{run_id}/cancel")
    async def run_cancel(run_id: str):
        try:
            return service.cancel_run(run_id)
        except EngineServiceError as error:
            return _service_error(error)

    @router.post("/api/desktop-elements/capture")
    async def desktop_element_capture(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        delay = body.get("delay_seconds", 3) if isinstance(body, dict) else 3
        try:
            return await service.capture_desktop_element(delay)
        except EngineServiceError as error:
            return _service_error(error)

    @router.post("/api/desktop-elements/check")
    async def desktop_element_check(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        selector = body.get("selector") if isinstance(body, dict) else None
        try:
            return await service.check_desktop_element(selector)
        except EngineServiceError as error:
            return _service_error(error)

    @router.get("/api/engine/logs")
    async def engine_logs(lines: int = 200):
        return service.logs(lines)

    @router.get("/api/events")
    async def event_stream(request: Request):
        async def generate():
            subscription = events.subscribe()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(
                            subscription.queue.get(),
                            timeout=15,
                        )
                        if event is None:
                            break
                        yield f"event: {event['type']}\n"
                        yield "data: " + json.dumps(
                            event["data"],
                            ensure_ascii=False,
                        ) + "\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                events.unsubscribe(subscription)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
