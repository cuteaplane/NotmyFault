from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from notmyfault.host.api.auth import ApiTokenStore


DASHBOARD_ORIGINS = tuple(
    f"http://{host}:{port}"
    for host in ("127.0.0.1", "localhost")
    for port in range(19199, 19219)
)


def install_api_middleware(app: FastAPI, token_store: ApiTokenStore) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DASHBOARD_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if not request.url.path.startswith("/api/") or request.method == "OPTIONS":
            return await call_next(request)
        authorization = request.headers.get("Authorization", "")
        token = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ")
            else ""
        )
        if not token and request.url.path == "/api/events":
            token = request.query_params.get("token", "")
        if not token_store.matches(token):
            token_store.repair_file()
            return JSONResponse(
                {"detail": "Forbidden: invalid API Token"}, status_code=403
            )
        return await call_next(request)
