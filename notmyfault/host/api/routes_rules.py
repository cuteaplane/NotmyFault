import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from notmyfault.host.api.services.rule_runs import (
    RuleRunService,
    RuleRunServiceError,
)
from notmyfault.host.api.services.rules import RuleService, RuleServiceError


_TEST_CONTEXT_MAX_BYTES = 1024 * 1024


def create_rules_router(
    service: RuleService,
    run_service: RuleRunService,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/rules")
    async def rules_list():
        return service.list_rules()

    @router.post("/api/rules/validate")
    async def rules_validate(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "无效的 JSON 请求体"},
                status_code=400,
            )
        if not isinstance(body, dict) or "rule" not in body:
            return JSONResponse(
                {"ok": False, "error": "请求体必须包含 rule 对象"},
                status_code=400,
            )
        return service.validate_draft(body["rule"])

    @router.post("/api/rules/approve")
    async def rules_approve(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "无效的 JSON 请求体"},
                status_code=400,
            )
        rules = body.get("rules") if isinstance(body, dict) else None
        password = (
            body.get("admin_key_password") if isinstance(body, dict) else None
        )
        try:
            return service.approve(rules, password)
        except RuleServiceError as error:
            return JSONResponse(error.body, status_code=error.status_code)

    @router.put("/api/rules")
    async def rules_save(request: Request):
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
            return service.save(
                body.get("rules"),
                body.get("admin_key_password"),
            )
        except RuleServiceError as error:
            return JSONResponse(error.body, status_code=error.status_code)

    @router.post("/api/rules/{rule_index}/run")
    async def rules_run(rule_index: int, request: Request):
        raw_body = await request.body()
        if len(raw_body) > _TEST_CONTEXT_MAX_BYTES:
            return JSONResponse(
                {"ok": False, "error": "测试数据超过 1 MiB 上限"},
                status_code=413,
            )
        body = None
        if raw_body:
            try:
                body = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
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
            return run_service.run(rule_index, body)
        except RuleRunServiceError as error:
            return JSONResponse(error.body, status_code=error.status_code)

    return router
