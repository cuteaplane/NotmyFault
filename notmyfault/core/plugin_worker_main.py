import json
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKER_DIR = str(Path(__file__).resolve().parent)
try:
    sys.path.remove(_WORKER_DIR)
except ValueError:
    pass
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import notmyfault as _notmyfault
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.value_codec import decode_value, encode_value
from notmyfault.core.workflow import invoke_action
from notmyfault.security.plugin_imports import PluginImports
from notmyfault.security.plugin_loader import inspect_plugin_tree


def main() -> None:
    protocol = sys.stdout
    protocol.write(json.dumps({"type": "ready", "protocol": 1}) + "\n")
    protocol.flush()
    try:
        request = json.loads(sys.stdin.readline())
    except (json.JSONDecodeError, OSError):
        sys.exit(2)

    sys.stdout = sys.stderr
    try:
        typed = request.get("value_encoding") == "typed-v1"
        if typed:
            request = decode_value(request)
        entry = Path(request["entry"])
        plugin_root = Path(request["plugin_root"]).resolve()
        tree = inspect_plugin_tree(str(plugin_root))
        if tree is None or tree.file_snapshot != request.get("file_snapshot"):
            raise PermissionError("隔离动作插件文件完整性校验失败")
        relative_entry = entry.resolve().relative_to(plugin_root).as_posix()
        expected_hash = request.get("entry_sha256")
        if (
            not isinstance(expected_hash, str)
            or tree.file_snapshot.get(relative_entry) != expected_hash
        ):
            raise PermissionError("隔离动作入口完整性校验失败")
        importer = PluginImports(str(plugin_root), "isolated_action", tree.py_sources)
        module = importer.load_entry(str(entry))
        action_info = request["action_info"]
        context = request.get("context", {})
        if typed:
            context["_type_registry"] = TypeRegistry(request.get("data_types"))
        result = invoke_action(module.run, module, action_info, request["params"], context)
        if typed:
            result = encode_value(result)
        else:
            json.dumps(result, allow_nan=False)
        payload = {"type": "result", "ok": True, "result": result}
        if typed:
            payload["value_encoding"] = "typed-v1"
    except BaseException as exc:  # 动作代码什么都能抛，包括 SystemExit
        payload = {
            "type": "result",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    sys.stdout = protocol
    protocol.write(json.dumps(payload, ensure_ascii=False) + "\n")
    protocol.flush()


if __name__ == "__main__":
    main()
