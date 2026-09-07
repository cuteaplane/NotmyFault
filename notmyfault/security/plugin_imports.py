"""插件源码使用独立模块名，入口和兄弟模块都执行验签时读取的内容。"""

import builtins
import importlib.util
import sys
import threading
import types
from pathlib import Path


class PluginImports:
    def __init__(self, root: str, namespace: str, sources: dict[str, bytes]) -> None:
        self.root = Path(root).resolve()
        self.namespace = namespace
        self.sources = {
            Path(path).resolve().relative_to(self.root).as_posix(): source
            for path, source in sources.items()
        }
        self.modules: dict[str, types.ModuleType] = {}
        self._lock = threading.RLock()
        self._builtins = {**vars(builtins), "__import__": self._import}

    def _has_module(self, relative: str) -> bool:
        path = relative.replace(".", "/")
        return path + ".py" in self.sources or any(
            name.startswith(path + "/") for name in self.sources
        )

    def _import(self, name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            package = globals.get("__package__", "") if globals else ""
            fullname = importlib.util.resolve_name("." * level + name, package)
            internal = fullname == self.namespace or fullname.startswith(self.namespace + ".")
        else:
            internal = self._has_module(name.split(".", 1)[0])
            fullname = f"{self.namespace}.{name}" if internal else name
        if not internal:
            return builtins.__import__(name, globals, locals, fromlist, level)
        with self._lock:
            module = self._load(fullname)
            for item in fromlist or ():
                names = getattr(module, "__all__", ()) if item == "*" else (item,)
                for child in names:
                    child_name = fullname + "." + child
                    relative = child_name[len(self.namespace) + 1:]
                    if not hasattr(module, child) and self._has_module(relative):
                        self._load(child_name)
            if fromlist or level:
                return module
            return self._load(self.namespace + "." + name.split(".", 1)[0])

    def _load(self, fullname: str) -> types.ModuleType:
        if fullname in self.modules:
            return self.modules[fullname]
        relative = fullname[len(self.namespace) + 1:].replace(".", "/")
        package_path = relative + "/__init__.py"
        source_path = relative + ".py"
        if package_path in self.sources:
            return self._execute(fullname, package_path, True)
        if source_path in self.sources:
            return self._execute(fullname, source_path, False)
        if any(path.startswith(relative + "/") for path in self.sources):
            return self._execute(fullname, None, True)
        raise ModuleNotFoundError(f"插件中没有模块: {fullname}", name=fullname)

    def _execute(self, fullname, relative, is_package):
        parent_name, _, child = fullname.rpartition(".")
        parent = self._load(parent_name) if fullname != self.namespace else None
        module = types.ModuleType(fullname)
        directory = self.root / fullname[len(self.namespace) + 1:].replace(".", "/")
        module.__package__ = fullname if is_package else parent_name
        module.__builtins__ = self._builtins
        module.__spec__ = importlib.util.spec_from_loader(fullname, loader=None, is_package=is_package)
        if is_package:
            module.__path__ = [str(directory)]
        self.modules[fullname] = module
        sys.modules[fullname] = module
        try:
            if relative is not None:
                module.__file__ = str(self.root / relative)
                exec(compile(self.sources[relative], module.__file__, "exec"), module.__dict__)
        except BaseException:
            self.modules.pop(fullname, None)
            sys.modules.pop(fullname, None)
            raise
        if parent is not None:
            setattr(parent, child, module)
        return module

    def load_entry(self, path: str) -> types.ModuleType:
        relative = Path(path).resolve().relative_to(self.root).as_posix()
        with self._lock:
            return self._execute(self.namespace, relative, True)

    def load_file(self, path: str) -> types.ModuleType:
        relative = Path(path).resolve().relative_to(self.root).with_suffix("").as_posix()
        if relative.endswith("/__init__"):
            relative = relative[:-len("/__init__")]
        with self._lock:
            return self._load(self.namespace + "." + relative.replace("/", "."))

    def close(self) -> None:
        for name, module in self.modules.items():
            if sys.modules.get(name) is module:
                sys.modules.pop(name, None)
        self.modules.clear()
