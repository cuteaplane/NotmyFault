"""索引插件清单里的贡献项和已经加载的命令。"""

import copy
import os
import sys
import threading
from typing import Any, Callable, Dict, Optional


CONTRIBUTION_KINDS = ("commands", "parameter_editors", "views", "data_types")


class ExtensionRegistry:
    """按插件保存贡献清单、命令函数和扩展模块。"""

    def __init__(self) -> None:
        self._plugins: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, Dict[str, Callable[..., Any]]] = {}
        self._modules: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def register_manifest(
        self,
        plugin_id: str,
        kind: str,
        meta: Dict[str, Any],
        root: str,
    ) -> None:
        contributes = meta.get("contributes")
        if not isinstance(contributes, dict):
            self.unregister_plugin(plugin_id)
            return
        indexed: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for contribution_kind in CONTRIBUTION_KINDS:
            indexed[contribution_kind] = {
                item["id"]: copy.deepcopy(item)
                for item in contributes.get(contribution_kind, [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        with self._lock:
            self._plugins[plugin_id] = {
                "kind": kind,
                "meta": meta,
                "root": os.path.realpath(root),
                "contributions": indexed,
            }

    def register_command(
        self,
        plugin_id: str,
        command_id: str,
        handler: Callable[..., Any],
        module: Any,
    ) -> None:
        with self._lock:
            self._handlers.setdefault(plugin_id, {})[command_id] = handler
            self._modules.setdefault(plugin_id, {})[getattr(module, "__name__", command_id)] = module

    def unregister_plugin(self, plugin_id: str) -> None:
        with self._lock:
            self._plugins.pop(plugin_id, None)
            self._handlers.pop(plugin_id, None)
            modules = self._modules.pop(plugin_id, {})
        for module in modules.values():
            sys.modules.pop(getattr(module, "__name__", ""), None)

    def clear(self) -> None:
        with self._lock:
            plugin_ids = tuple(set(self._plugins) | set(self._handlers) | set(self._modules))
        for plugin_id in plugin_ids:
            self.unregister_plugin(plugin_id)

    def plugin(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._plugins.get(plugin_id)

    def contribution(
        self, plugin_id: str, kind: str, contribution_id: str
    ) -> Optional[Dict[str, Any]]:
        plugin = self.plugin(plugin_id)
        if plugin is None:
            return None
        return plugin["contributions"].get(kind, {}).get(contribution_id)

    def command(self, plugin_id: str, command_id: str) -> Optional[Dict[str, Any]]:
        return self.contribution(plugin_id, "commands", command_id)

    def handler(
        self, plugin_id: str, command_id: str
    ) -> Optional[Callable[..., Any]]:
        with self._lock:
            return self._handlers.get(plugin_id, {}).get(command_id)

    def view(self, plugin_id: str, view_id: str) -> Optional[Dict[str, Any]]:
        return self.contribution(plugin_id, "views", view_id)

    def data_type(self, plugin_id: str, data_type_id: str) -> Optional[Dict[str, Any]]:
        return self.contribution(plugin_id, "data_types", data_type_id)

    def parameter_editor(
        self, plugin_id: str, editor_id: str
    ) -> Optional[Dict[str, Any]]:
        return self.contribution(plugin_id, "parameter_editors", editor_id)

    def view_page_path(self, plugin_id: str, view_id: str) -> Optional[str]:
        plugin = self.plugin(plugin_id)
        view = self.view(plugin_id, view_id)
        if plugin is None or view is None:
            return None
        root = plugin["root"]
        page = os.path.realpath(os.path.join(root, view["page"]))
        try:
            inside = os.path.commonpath((page, root)) == root
        except ValueError:
            inside = False
        return page if inside and os.path.isfile(page) else None

    def source_commands(
        self, plugin_id: str, source_kind: str, source_id: str
    ) -> Optional[set[str]]:
        source = self.contribution(plugin_id, source_kind, source_id)
        if source is None:
            return None
        commands: set[str] = set()
        command = source.get("command")
        if isinstance(command, str):
            commands.add(command)
        view_id = source.get("view")
        view = self.view(plugin_id, view_id) if isinstance(view_id, str) else None
        if view is not None:
            commands.update(
                command_id
                for command_id in view.get("commands", [])
                if isinstance(command_id, str)
            )
        return commands

    def public_contributions(self) -> Dict[str, list[Dict[str, Any]]]:
        result = {kind: [] for kind in CONTRIBUTION_KINDS}
        with self._lock:
            plugins = list(self._plugins.items())
            handlers = {
                plugin_id: set(plugin_handlers)
                for plugin_id, plugin_handlers in self._handlers.items()
            }
        for plugin_id, plugin in sorted(plugins):
            meta = plugin["meta"]
            for kind in CONTRIBUTION_KINDS:
                for item in plugin["contributions"].get(kind, {}).values():
                    public = copy.deepcopy(item)
                    public.update({
                        "plugin_id": plugin_id,
                        "plugin_kind": plugin["kind"],
                        "plugin_name": meta.get("name", plugin_id),
                        "package_name": meta.get("package_name", ""),
                        "qualified_id": f"{meta.get('package_name', plugin_id)}/{kind}/{item['id']}",
                    })
                    if kind == "commands":
                        public.pop("handler", None)
                        public["loaded"] = item["id"] in handlers.get(plugin_id, set())
                    result[kind].append(public)
        return result
