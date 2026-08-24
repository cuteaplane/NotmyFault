from __future__ import annotations

from typing import Any

import uvicorn

from notmyfault.application_paths import ApplicationPaths
from notmyfault.config import SignedConfigStore
from notmyfault.core.run_history import RunHistory
from notmyfault.host.api.application import ApiApplication
from notmyfault.host.api.auth import ApiTokenStore
from notmyfault.host.api.events import EventBroker
from notmyfault.host.api.plugin_installation import (
    PendingPreviewStore,
    PluginFileSystem,
    PluginTemporaryStorage,
)
from notmyfault.host.api.ports import (
    DesktopElementPort,
    EngineControlPort,
    PluginRegistryPort,
)


class ApiServer:
    def __init__(self, application: ApiApplication) -> None:
        self.app = application.app
        self._application = application
        self._server: uvicorn.Server | None = None

    def publish_event(self, event_type: str, data: dict) -> None:
        self._application.publish_event(event_type, data)

    def serve(
        self,
        host: str = "127.0.0.1",
        port: int = 19198,
        sockets: list | None = None,
    ) -> None:
        config = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            access_log=False,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        try:
            self._server.run(sockets=sockets)
        except OSError as error:
            code = getattr(error, "winerror", None)
            if not (
                str(code) == "10048"
                or "10048" in str(error)
                or "bind" in str(error).lower()
            ):
                raise
            print(f"\n[提示] 端口 {port} 已被占用 — 引擎可能已在运行")
        except SystemExit:
            print("\n[API] HTTP 服务已退出")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


def create_api_server(
    engine_runner: EngineControlPort,
    store: SignedConfigStore,
    paths: ApplicationPaths,
    *,
    token_store: ApiTokenStore,
    ai_key_store: Any,
    plugin_file_system: PluginFileSystem,
    pending_previews: PendingPreviewStore,
    plugin_temporary_storage: PluginTemporaryStorage,
    desktop_elements: DesktopElementPort,
    plugin_registry: PluginRegistryPort,
    run_history: RunHistory,
    event_broker: EventBroker,
    ai_draft_provider: Any = None,
) -> ApiServer:
    application = ApiApplication(
        engine_runner=engine_runner,
        store=store,
        paths=paths,
        token_store=token_store,
        ai_key_store=ai_key_store,
        plugin_file_system=plugin_file_system,
        pending_previews=pending_previews,
        plugin_temporary_storage=plugin_temporary_storage,
        desktop_elements=desktop_elements,
        plugin_registry=plugin_registry,
        run_history=run_history,
        event_broker=event_broker,
        ai_draft_provider=ai_draft_provider,
    )
    return ApiServer(application)
