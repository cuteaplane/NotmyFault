import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from notmyfault.host.api.services.plugin_catalog import PluginCatalogService
from notmyfault.host.api.services.plugin_installation import (
    PluginInstallationError,
    PluginInstallationService,
)


def create_plugins_router(
    catalog: PluginCatalogService,
    installation: PluginInstallationService,
) -> APIRouter:
    router = APIRouter()
    install_slots = asyncio.Semaphore(2)

    async def read_upload(file: UploadFile) -> bytes | None:
        limit = installation.max_upload_bytes
        content = bytearray()
        while True:
            chunk = await file.read(min(1024 * 1024, limit - len(content) + 1))
            if not chunk:
                return bytes(content)
            content.extend(chunk)
            if len(content) > limit:
                return None

    @router.get("/api/plugins")
    async def plugin_schema():
        return catalog.schema()

    @router.get("/api/plugins/list")
    async def plugin_list():
        return catalog.list_all()

    @router.post("/api/plugins/registry")
    async def plugin_registry(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "读取插件索引失败"},
                status_code=400,
            )
        try:
            return await asyncio.to_thread(
                installation.registry,
                body.get("url", "") if isinstance(body, dict) else ""
            )
        except PluginInstallationError as error:
            return JSONResponse(error.body, status_code=error.status_code)

    @router.post("/api/plugins/registry/download")
    async def plugin_registry_download(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = None
        try:
            artifact = await asyncio.to_thread(installation.download_registry, body)
        except PluginInstallationError as error:
            return JSONResponse(error.body, status_code=error.status_code)
        return Response(
            artifact.content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{artifact.filename}"'
                ),
                "X-Plugin-SHA256": artifact.sha256,
            },
        )

    @router.post("/api/plugins/toggle")
    async def plugin_toggle(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return installation.toggle(
                body.get("type", "") if isinstance(body, dict) else "",
                body.get("id", "") if isinstance(body, dict) else "",
            )
        except PluginInstallationError as error:
            return JSONResponse(error.body, status_code=error.status_code)

    @router.post("/api/plugins/preview")
    async def plugin_preview(request: Request):
        form = await request.form()
        file = form.get("file")
        if not isinstance(file, UploadFile):
            return JSONResponse(
                {"ok": False, "error": "缺少上传文件"},
                status_code=400,
            )
        password = form.get("password", "")
        if not isinstance(password, str):
            password = ""
        data = await read_upload(file)
        if data is None:
            return JSONResponse(
                {"ok": False, "error": "插件包超过上传大小限制"},
                status_code=413,
            )
        try:
            async with install_slots:
                return await asyncio.to_thread(installation.preview, data, password)
        except PluginInstallationError as error:
            return JSONResponse(error.body, status_code=error.status_code)

    @router.post("/api/plugins/install")
    async def plugin_install(request: Request):
        form = await request.form()
        preview_token = str(form.get("preview_token", "") or "")
        password = str(form.get("password", "") or "")
        signing_password = str(form.get("signing_password", "") or "")
        confirmed_risk_ids = str(form.get("confirmed_risk_ids", "") or "")
        force = str(form.get("force", "")).lower() in ("1", "true", "yes")
        upload = form.get("file")
        data = await read_upload(upload) if isinstance(upload, UploadFile) else b""
        if data is None:
            return JSONResponse(
                {"ok": False, "error": "插件包超过上传大小限制"},
                status_code=413,
            )
        try:
            async with install_slots:
                return await asyncio.to_thread(
                    installation.install,
                    data or None,
                    preview_token=preview_token,
                    password=password,
                    signing_password=signing_password,
                    force=force,
                    confirmed_risk_ids=confirmed_risk_ids,
                )
        except PluginInstallationError as error:
            return JSONResponse(error.body, status_code=error.status_code)

    @router.get("/api/plugins/key-status")
    async def plugin_key_status():
        return installation.key_status()

    @router.delete("/api/plugins/{ptype}/{pid}")
    async def plugin_uninstall(ptype: str, pid: str):
        try:
            return installation.uninstall(ptype, pid)
        except PluginInstallationError as error:
            return JSONResponse(error.body, status_code=error.status_code)

    return router
