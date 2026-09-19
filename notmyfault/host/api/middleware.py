from __future__ import annotations

import collections
import asyncio
import threading
import time

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
    clients: dict[tuple[str, str], collections.deque[float]] = {}
    request_times_lock = threading.Lock()
    rate_window_seconds = 10.0
    rate_limit = 300
    last_token_check = 0.0

    app.add_middleware(
        CORSMiddleware,
        allow_origins=DASHBOARD_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        nonlocal last_token_check
        if not request.url.path.startswith("/api/") or request.method == "OPTIONS":
            return await call_next(request)
        authorization = request.headers.get("Authorization", "")
        token = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ")
            else ""
        )
        if not token_store.matches(token):
            return JSONResponse(
                {"detail": "Forbidden: invalid API Token"}, status_code=403
            )
        if request.method in {"POST", "PUT", "PATCH"}:
            limit = (65 if request.url.path in {"/api/plugins/preview", "/api/plugins/install"} else 1) * 1024 * 1024
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    return JSONResponse({"ok": False, "error": "请求体超过大小上限"}, status_code=413)
                body.extend(chunk)
            request._body = bytes(body)
        now = time.monotonic()
        with request_times_lock:
            cutoff = now - rate_window_seconds
            for key in list(clients):
                if not clients[key] or clients[key][-1] <= cutoff:
                    clients.pop(key)
            origin = request.headers.get("origin", "")
            client_key = (request.client.host if request.client else "", origin if origin in DASHBOARD_ORIGINS else "")
            request_times = clients.setdefault(client_key, collections.deque())
            while request_times and request_times[0] <= cutoff:
                request_times.popleft()
            if len(request_times) >= rate_limit:
                return JSONResponse(
                    {"detail": "Too Many Requests"},
                    status_code=429,
                    headers={"Retry-After": "10"},
                )
            request_times.append(now)
        if now - last_token_check >= 1.0:
            last_token_check = now
            await asyncio.to_thread(token_store.repair_file)
        return await call_next(request)
