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
    return JSONResponse(
        {"ok": False, "error": str(error)},
        status_code=status_code,
    )


def create_settings_router(service: SettingsService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/config/security-status")
    async def config_security_status():
        return service.security_status()

    @router.get("/api/settings/admin-authorization")
    async def admin_authorization_setting():
        return service.admin_authorization()

    @router.put("/api/settings/admin-authorization")
    async def update_admin_authorization_setting(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = None
        mode = body.get("mode") if isinstance(body, dict) else None
        try:
            return service.update_admin_authorization(mode)
        except SettingsServiceError as error:
            return _settings_error(error)

    @router.get("/api/settings/admin-rule-verification")
    async def admin_rule_verification_setting():
        return service.admin_rule_verification()

    @router.put("/api/settings/admin-rule-verification")
    async def update_admin_rule_verification_setting(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = None
        value = body.get("key_verification") if isinstance(body, dict) else None
        try:
            return service.update_admin_rule_verification(value)
        except SettingsServiceError as error:
            return _settings_error(error)

    @router.post("/api/config/security-approve")
    async def config_security_approve():
        try:
            return service.approve_security()
        except SettingsServiceError as error:
            return _settings_error(error)
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "重新签名配置失败"},
                status_code=500,
            )

    return router
