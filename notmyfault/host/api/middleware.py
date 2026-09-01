from __future__ import annotations

import collections
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
    request_times: collections.deque[float] = collections.deque()
    request_times_lock = threading.Lock()
    rate_window_seconds = 10.0
    rate_limit = 300

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
        if not token_store.matches(token):
            return JSONResponse(
                {"detail": "Forbidden: invalid API Token"}, status_code=403
            )
        token_store.repair_file()
        now = time.monotonic()
        with request_times_lock:
            cutoff = now - rate_window_seconds
            while request_times and request_times[0] <= cutoff:
                request_times.popleft()
            if len(request_times) >= rate_limit:
                return JSONResponse(
                    {"detail": "Too Many Requests"},
                    status_code=429,
                    headers={"Retry-After": "10"},
                )
            request_times.append(now)
        return await call_next(request)
