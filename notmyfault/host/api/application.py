from typing import Any, Dict

from fastapi import FastAPI

from notmyfault.application_paths import ApplicationPaths
from notmyfault.components.session import ComponentSessionManager
from notmyfault.config import SignedConfigStore
from notmyfault.core.run_history import RunHistory
from notmyfault.extensions.session import ExtensionSessionManager
from notmyfault.host.api.auth import ApiTokenStore
from notmyfault.host.api.events import EventBroker
from notmyfault.host.api.middleware import install_api_middleware
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
from notmyfault.host.api.routes_ai import create_ai_router
from notmyfault.host.api.routes_engine import create_engine_router
from notmyfault.host.api.routes_interactions import create_interactions_router
from notmyfault.host.api.routes_plugins import create_plugins_router
from notmyfault.host.api.routes_rules import create_rules_router
from notmyfault.host.api.routes_settings import create_settings_router
from notmyfault.host.api.services.ai_drafting import AIDraftingService
from notmyfault.host.api.services.engine import EngineService
from notmyfault.host.api.services.interactions import PluginInteractionService
from notmyfault.host.api.services.plugin_catalog import PluginCatalogService
from notmyfault.host.api.services.plugin_installation import PluginInstallationService
from notmyfault.host.api.services.rule_runs import RuleRunService
from notmyfault.host.api.services.rules import RuleService
from notmyfault.host.api.services.settings import SettingsService
from notmyfault.version import __version__


class ApiApplication:
    def __init__(
        self,
        engine_runner: EngineControlPort,
        store: SignedConfigStore,
        paths: ApplicationPaths,
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
    ):
        self._engine = engine_runner
        self._store = store
        self._paths = paths
        self._token_store = token_store
        self._plugin_file_system = plugin_file_system
        self._run_history = run_history
        self._events = event_broker
        self._component_sessions = ComponentSessionManager()
        self._extension_sessions = ExtensionSessionManager()
        self._plugin_catalog = PluginCatalogService(
            self._paths,
            self._store,
            self._engine,
        )
        self._ai_draft_provider = ai_draft_provider
        self._ai_key_store = ai_key_store
        self._pending_previews = pending_previews
        self._plugin_temporary_storage = plugin_temporary_storage
        self._desktop_elements = desktop_elements
        self._plugin_registry = plugin_registry

        self.app = FastAPI(title="NotmyFault Engine API", version=__version__)
        install_api_middleware(self.app, self._token_store)
        self._setup_routes()

    def publish_event(self, event_type: str, data: Dict[str, Any]):
        self._events.publish(event_type, data)

    def _setup_routes(self):
        plugin_catalog = self._plugin_catalog
        rule_service = RuleService(self._store, plugin_catalog.schema)
        self.app.include_router(
            create_engine_router(
                EngineService(
                    self._engine,
                    self._store,
                    self._paths,
                    self._run_history,
                    self._component_sessions,
                    self._extension_sessions,
                    self._desktop_elements,
                ),
                self._events,
            )
        )
        self.app.include_router(
            create_interactions_router(
                PluginInteractionService(
                    self._engine,
                    self._component_sessions,
                    self._extension_sessions,
                )
            )
        )
        self.app.include_router(
            create_settings_router(SettingsService(self._store, self._engine))
        )
        self.app.include_router(
            create_plugins_router(
                plugin_catalog,
                PluginInstallationService(
                    self._paths,
                    self._store,
                    plugin_catalog,
                    self._plugin_file_system,
                    self._pending_previews,
                    self._plugin_registry,
                    temporary_storage=self._plugin_temporary_storage,
                ),
            )
        )
        self.app.include_router(
            create_rules_router(
                rule_service,
                RuleRunService(self._engine, self._store),
            )
        )
        self.app.include_router(
            create_ai_router(
                self._create_ai_service(plugin_catalog, rule_service)
            )
        )

    def _create_ai_service(
        self,
        plugin_catalog: PluginCatalogService,
        rule_service: RuleService,
    ) -> AIDraftingService:
        return AIDraftingService(
            self._store,
            plugin_catalog.schema,
            rule_service.validate_draft,
            self._ai_key_store,
            provider=self._ai_draft_provider,
        )
