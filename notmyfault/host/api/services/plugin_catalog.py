from __future__ import annotations

import os
from typing import Any, Dict

from notmyfault.application_paths import ApplicationPaths
from notmyfault.config import ConfigValidationError, SignedConfigStore
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.data_types import DataTypeError
from notmyfault.host.api.ports import EngineControlPort
from notmyfault.platform.capabilities import probe_capabilities
from notmyfault.security.plugin_checks import (
    inspect_plugin, inspect_plugin_metadata, inspection_report,
    is_plugin_platform_compatible, plugin_directories,
)
from notmyfault.security.security import detect_security_mode
from notmyfault.security.plugins import load_plugin_manifest
from notmyfault.security.plugin_schema import scan_plugins

# 诊断里的存储名对应哪个目录分组
_STORE_CATEGORIES = {
    "trigger": "triggers",
    "triggers": "triggers",
    "action": "actions",
    "actions": "actions",
}


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
        result = self._scan(include_checks=False)
        try:
            result["data_types"] = TypeRegistry.from_plugins(result["triggers"], result["actions"], include_disabled=True).catalog()
        except DataTypeError as error:
            result["data_types"] = {**TypeRegistry().catalog(), "error": error.as_dict()}
        return result

    def list_all(self) -> Dict[str, Any]:
        return self._scan(include_checks=True)

    def _scan(self, *, include_checks: bool) -> Dict[str, Any]:
        base = str(self.paths.package_root)
        capability_report = probe_capabilities()
        user_dir = str(self.paths.user_plugins_dir)
        config = self._load_config()
        disabled = config.get("disabled_plugins", {})
        if not isinstance(disabled, dict):
            disabled = {"triggers": [], "actions": []}

        result: Dict[str, Dict[str, Any]] = {"triggers": {}, "actions": {}}
        # (分组, 插件 id) -> 这次扫描见过它的来源，用来判断诊断里的 id 能不能认领
        origins: Dict[tuple, set] = {}
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
                origins.setdefault((plugin_kind, plugin_id), set()).add("builtin")
            if include_checks:
                self._include_unscannable_plugins(
                    result[plugin_kind],
                    os.path.join(base, plugin_kind),
                    json_name,
                    "builtin",
                    plugin_kind,
                    capability_report,
                    origins,
                )
            if os.path.isdir(user_dir):
                for plugin_id, meta in scan_plugins(
                    user_dir,
                    plugin_kind,
                    json_name,
                ).items():
                    meta["origin"] = "user"
                    if plugin_id not in result[plugin_kind]:
                        result[plugin_kind][plugin_id] = meta
                    origins.setdefault((plugin_kind, plugin_id), set()).add("user")
                if include_checks:
                    self._include_unscannable_plugins(
                        result[plugin_kind],
                        os.path.join(user_dir, plugin_kind),
                        json_name,
                        "user",
                        plugin_kind,
                        capability_report,
                        origins,
                    )
            for plugin_id, meta in result[plugin_kind].items():
                meta["enabled"] = plugin_id not in disabled_set

        for plugin_kind in ("triggers", "actions"):
            for meta in result[plugin_kind].values():
                if isinstance(meta, dict):
                    self._annotate_availability(meta, capability_report)
        if include_checks:
            runtime = self._runtime_state()
            result["runtime"] = runtime
            for item in runtime["plugin_errors"]:
                category = _STORE_CATEGORIES.get(item["store"].lower())
                if category is None:
                    # 存储名不认识，只留在会话错误里，不往插件条目上挂
                    continue
                entry = self._error_target(result, origins, category, item["plugin_id"])
                if entry is not None:
                    entry["_error"] = item["reason"]
        if config.get("config_error"):
            result["config_error"] = config["config_error"]
            for plugin_kind in ("triggers", "actions"):
                for meta in result[plugin_kind].values():
                    meta["enabled"] = False
                    meta["_error"] = config["config_error"]
        return result

    def _error_target(self, result: Dict[str, Any], origins: Dict[tuple, set], category: str, plugin_id: str):
        """诊断只带 id；同一个 id 这次扫描在内置和用户目录都出现过时不认领"""
        entry = result[category].get(plugin_id)
        if entry is None:
            return None
        if len(origins.get((category, plugin_id), ())) > 1:
            return None
        return entry

    def _runtime_state(self) -> Dict[str, Any]:
        """本次引擎会话的插件错误；目录条目只表示扫描结果"""
        try:
            state = str(self._engine.engine_state or "stopped")
        except Exception:
            state = "stopped"
        engine = self._engine.current_engine
        if engine is None:
            return {
                "engine_state": state,
                "diagnostics_available": False,
                "plugin_errors": [],
                "plugin_error_count": 0,
            }
        try:
            raw = engine.get_diagnostics().get("plugins", {}).get("errors", []) or []
        except Exception as error:
            # 读不到诊断时不能显示成"零错误"
            return {
                "engine_state": state,
                "diagnostics_available": False,
                "diagnostics_error": type(error).__name__,
                "plugin_errors": [],
                "plugin_error_count": 0,
            }
        errors = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            errors.append({
                "store": str(item[0]),
                "plugin_id": str(item[1]),
                "reason": str(item[2]),
            })
        return {
            "engine_state": state,
            "diagnostics_available": True,
            "plugin_errors": errors,
            "plugin_error_count": len(errors),
        }

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
        for kind in ("triggers", "actions"):
            builtin = self.find_builtin_plugin_by_id(kind, plugin_id)
            if builtin is not None:
                return builtin
        existing = self.find_user_plugin_by_id(plugin_id)
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
            ).items():
                if predicate(plugin_id, meta):
                    return plugin_kind, plugin_id, meta
        return None

    def _load_config(self) -> Dict[str, Any]:
        try:
            return self._store.load_verified_config()
        except ConfigValidationError as error:
            return {"config_error": str(error)}

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

    def _include_unscannable_plugins(
        self,
        result: Dict[str, Any],
        plugin_root: str,
        json_name: str,
        origin: str,
        plugin_kind: str,
        capability_report: Dict[str, Dict[str, Any]],
        origins: Dict[tuple, set],
    ) -> None:
        kind = "trigger" if plugin_kind == "triggers" else "action"
        mode = detect_security_mode()
        installed = load_plugin_manifest(self.paths.plugin_manifest_file) if origin == "user" else {}
        for folder in plugin_directories(plugin_root):
            if not (folder / json_name).is_file():
                continue
            meta, errors, schema_errors = inspect_plugin_metadata(folder, kind)
            if errors:
                # JSON 读不出来或解析失败：身份无法确认，只按目录名展示，界面不给启禁和卸载
                result.setdefault(folder.name, {
                    "id": folder.name,
                    "name": folder.name,
                    "origin": origin,
                    "_error": "; ".join(str(item) for item in errors[:2]),
                    "_unidentified": True,
                })
                origins.setdefault((plugin_kind, folder.name), set()).add(origin)
                continue
            plugin_id = meta.get("id", folder.name)
            existing = result.get(plugin_id)
            if existing is not None and existing.get("origin") != origin:
                origins.setdefault((plugin_kind, plugin_id), set()).add(origin)
                continue
            if schema_errors:
                result.setdefault(plugin_id, {
                    **meta, "id": plugin_id, "origin": origin,
                    "_error": "schema: " + "; ".join(schema_errors[:2]),
                })
                origins.setdefault((plugin_kind, plugin_id), set()).add(origin)
                continue
            inspection = inspect_plugin(folder, kind, origin, capability_report=capability_report, installed_manifest=installed)
            result.setdefault(plugin_id, {**meta, "origin": origin})["checks"] = inspection_report(inspection, mode)
            origins.setdefault((plugin_kind, plugin_id), set()).add(origin)
