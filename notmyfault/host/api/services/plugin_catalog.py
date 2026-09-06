from __future__ import annotations

import json
import os
from typing import Any, Dict

from notmyfault.application_paths import ApplicationPaths
from notmyfault.config import ConfigValidationError, SignedConfigStore
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.data_types import DataTypeError
from notmyfault.host.api.ports import EngineControlPort
from notmyfault.platform.capabilities import probe_capabilities
from notmyfault.security.plugin_loader import is_plugin_platform_compatible
from notmyfault.security.plugin_schema import scan_plugins, validate_plugin_meta


class PluginCatalogService:
    def __init__(
        self,
        paths: ApplicationPaths,
        store: SignedConfigStore,
        engine: EngineControlPort,
    ) -> None:
        self.paths = paths
        self._store = store
        self._engine = engine

    def schema(self) -> Dict[str, Any]:
        base = str(self.paths.package_root)
        capability_report = probe_capabilities()
        result: Dict[str, Dict[str, Any]] = {
            "triggers": scan_plugins(base, "triggers", "trigger.json"),
            "actions": scan_plugins(base, "actions", "action.json"),
        }
        for plugin_kind in ("triggers", "actions"):
            for meta in result[plugin_kind].values():
                meta["origin"] = "builtin"
        user_dir = str(self.paths.user_plugins_dir)
        if os.path.isdir(user_dir):
            for plugin_kind in ("triggers", "actions"):
                json_name = (
                    "trigger.json" if plugin_kind == "triggers" else "action.json"
                )
                for plugin_id, meta in scan_plugins(
                    user_dir,
                    plugin_kind,
                    json_name,
                    include_disabled=True,
                ).items():
                    if plugin_id not in result[plugin_kind]:
                        meta["origin"] = "user"
                        result[plugin_kind][plugin_id] = meta
        disabled = self._load_config().get("disabled_plugins", {})
        if not isinstance(disabled, dict):
            disabled = {}
        current_engine = self._engine.current_engine
        plugin_errors = []
        if current_engine is not None:
            plugin_errors = (
                current_engine.get_diagnostics()
                .get("plugins", {})
                .get("errors", [])
            )
        for plugin_kind in ("triggers", "actions"):
            disabled_set = set(disabled.get(plugin_kind, []))
            for plugin_id, meta in result[plugin_kind].items():
                if not isinstance(meta, dict):
                    continue
                if meta.get("origin") == "user":
                    meta["enabled"] = plugin_id not in disabled_set
                elif plugin_id in disabled_set:
                    meta["enabled"] = False
                for error in plugin_errors:
                    if len(error) < 3 or error[1] != plugin_id:
                        continue
                    category = (
                        "triggers" if error[0] == "Trigger" else "actions"
                    )
                    if category == plugin_kind:
                        meta.setdefault("_error", error[2])
                self._annotate_availability(meta, capability_report)
        try:
            result["data_types"] = TypeRegistry.from_plugins(result["triggers"], result["actions"], include_disabled=True).catalog()
        except DataTypeError as error:
            result["data_types"] = {**TypeRegistry().catalog(), "error": error.as_dict()}
        return result

    def list_all(self) -> Dict[str, Any]:
        base = str(self.paths.package_root)
        capability_report = probe_capabilities()
        user_dir = str(self.paths.user_plugins_dir)
        disabled = self._load_config().get("disabled_plugins", {})
        if not isinstance(disabled, dict):
            disabled = {"triggers": [], "actions": []}

        result: Dict[str, Dict[str, Any]] = {"triggers": {}, "actions": {}}
        for plugin_kind in ("triggers", "actions"):
            json_name = (
                "trigger.json" if plugin_kind == "triggers" else "action.json"
            )
            disabled_set = set(disabled.get(plugin_kind, []))
            for plugin_id, meta in scan_plugins(
                base,
                plugin_kind,
                json_name,
            ).items():
                meta["origin"] = "builtin"
                result[plugin_kind][plugin_id] = meta
            self._include_unscannable_plugins(
                result[plugin_kind],
                os.path.join(base, plugin_kind),
                json_name,
                "builtin",
                plugin_kind,
            )
            if os.path.isdir(user_dir):
                for plugin_id, meta in scan_plugins(
                    user_dir,
                    plugin_kind,
                    json_name,
                    include_disabled=True,
                ).items():
                    meta["origin"] = "user"
                    if plugin_id not in result[plugin_kind]:
                        result[plugin_kind][plugin_id] = meta
                self._include_unscannable_plugins(
                    result[plugin_kind],
                    os.path.join(user_dir, plugin_kind),
                    json_name,
                    "user",
                    plugin_kind,
                )
            for plugin_id in disabled_set:
                if plugin_id in result[plugin_kind]:
                    result[plugin_kind][plugin_id]["enabled"] = False
            for plugin_id, meta in result[plugin_kind].items():
                if meta.get("origin") == "user":
                    meta["enabled"] = plugin_id not in disabled_set

        for plugin_kind in ("triggers", "actions"):
            for meta in result[plugin_kind].values():
                if isinstance(meta, dict):
                    self._annotate_availability(meta, capability_report)
        current_engine = self._engine.current_engine
        if current_engine is not None:
            errors = (
                current_engine.get_diagnostics()
                .get("plugins", {})
                .get("errors", [])
            )
            for error in errors:
                if len(error) < 3:
                    continue
                category = "triggers" if error[0] == "Trigger" else "actions"
                plugin_id = error[1]
                if plugin_id in result[category]:
                    result[category][plugin_id]["_error"] = error[2]
        return result

    def find_user_plugin_by_package(self, package_name: str):
        if not package_name:
            return None
        return self._find_user_plugin(
            lambda plugin_id, meta: meta.get("package_name") == package_name
        )

    def find_user_plugin_by_id(self, plugin_id: str):
        if not plugin_id:
            return None
        return self._find_user_plugin(
            lambda found_id, meta: found_id == plugin_id
        )

    def find_builtin_plugin_by_id(self, plugin_kind: str, plugin_id: str):
        if plugin_kind not in ("triggers", "actions") or not plugin_id:
            return None
        base = str(self.paths.package_root)
        json_name = (
            "trigger.json" if plugin_kind == "triggers" else "action.json"
        )
        meta = scan_plugins(base, plugin_kind, json_name).get(plugin_id)
        return (plugin_kind, plugin_id, meta) if meta is not None else None

    def plugin_id_collision(self, plugin_kind: str, meta: Dict[str, Any]):
        plugin_id = meta.get("id", "")
        package_name = meta.get("package_name", "")
        builtin = self.find_builtin_plugin_by_id(plugin_kind, plugin_id)
        if builtin is not None:
            return builtin
        user_dir = str(self.paths.user_plugins_dir)
        json_name = (
            "trigger.json" if plugin_kind == "triggers" else "action.json"
        )
        existing_meta = scan_plugins(
            user_dir,
            plugin_kind,
            json_name,
            include_disabled=True,
        ).get(plugin_id)
        existing = (
            (plugin_kind, plugin_id, existing_meta)
            if existing_meta is not None
            else None
        )
        if existing and existing[2].get("package_name") != package_name:
            return existing
        return None

    def _find_user_plugin(self, predicate):
        user_dir = str(self.paths.user_plugins_dir)
        if not os.path.isdir(user_dir):
            return None
        for plugin_kind in ("triggers", "actions"):
            json_name = (
                "trigger.json" if plugin_kind == "triggers" else "action.json"
            )
            for plugin_id, meta in scan_plugins(
                user_dir,
                plugin_kind,
                json_name,
                include_disabled=True,
            ).items():
                if predicate(plugin_id, meta):
                    return plugin_kind, plugin_id, meta
        return None

    def _load_config(self) -> Dict[str, Any]:
        try:
            return self._store.load_verified_config()
        except ConfigValidationError:
            return {"rules": []}

    @staticmethod
    def _annotate_availability(
        meta: Dict[str, Any],
        capability_report: Dict[str, Dict[str, Any]],
    ) -> None:
        from notmyfault.platform.capabilities import (
            is_capability_compatible,
        )

        platform_compatible = is_plugin_platform_compatible(meta)
        capability_ok, problems = is_capability_compatible(
            meta,
            capability_report,
        )
        reasons = [
            f"{problem['capability']}: {problem['reason']}"
            for problem in problems
        ]
        meta["platform_compatible"] = platform_compatible
        meta["capability_compatible"] = capability_ok
        if not platform_compatible:
            meta["availability"] = "unavailable"
            meta["unavailable_reasons"] = ["当前平台不支持"]
        elif not capability_ok:
            meta["availability"] = "unavailable"
            meta["unavailable_reasons"] = reasons
        else:
            required = meta.get("requires_capabilities") or []
            degraded = [
                f"{capability_id}: {entry['reason']}"
                for capability_id, entry in capability_report.items()
                if entry["degraded"] and capability_id in required
            ]
            meta["availability"] = "partial" if degraded else "available"
            meta["unavailable_reasons"] = degraded

    @staticmethod
    def _include_unscannable_plugins(
        result: Dict[str, Any],
        plugin_root: str,
        json_name: str,
        origin: str,
        plugin_kind: str,
    ) -> None:
        if not os.path.isdir(plugin_root):
            return
        plugin_type = "trigger" if plugin_kind == "triggers" else "action"
        for folder_name in sorted(os.listdir(plugin_root)):
            json_path = os.path.join(plugin_root, folder_name, json_name)
            if not os.path.exists(json_path):
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as file:
                    meta = json.load(file)
            except (json.JSONDecodeError, OSError):
                continue
            plugin_id = meta.get("id", folder_name)
            if plugin_id in result:
                continue
            meta["origin"] = origin
            valid, errors = validate_plugin_meta(meta, plugin_type)
            if not valid:
                meta["_error"] = "schema: " + "; ".join(errors[:2])
            result[plugin_id] = meta
