from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from notmyfault.host.api.services.settings import (
    SettingsService,
    SettingsServiceError,
)


def _settings_error(error: SettingsServiceError) -> JSONResponse:
    status_code = 400
    if error.kind == "integrity":
        status_code = 409
    elif error.kind == "write":
        status_code = 500
    body = {"ok": False, "error": str(error)}
    if error.code:
        body["code"] = error.code
    return JSONResponse(body, status_code=status_code)


def create_settings_router(service: SettingsService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/config/security-status")
    async def config_security_status():
        return service.security_status()

    @router.post("/api/config/security-approve")
    async def config_security_approve(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = None
        password = (
            body.get("admin_key_password") if isinstance(body, dict) else None
        )
        try:
            return service.approve_security(password)
        except SettingsServiceError as error:
            return _settings_error(error)
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "重新签名配置失败"},
                status_code=500,
            )

    return router
