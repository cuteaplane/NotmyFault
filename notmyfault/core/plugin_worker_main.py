import json
import inspect
import sys
import threading
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKER_DIR = str(Path(__file__).resolve().parent)
try:
    sys.path.remove(_WORKER_DIR)
except ValueError:
    pass
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.value_codec import decode_value, encode_value
from notmyfault.core.workflow import invoke_action
from notmyfault.core.plugin_worker import MAX_EXTENSION_PROTOCOL_LINE, MAX_PROTOCOL_LINE
from notmyfault.security.plugin_imports import PluginImports
from notmyfault.security.plugin_loader import inspect_plugin_tree
from notmyfault.extensions.registry import ExtensionRegistry
from notmyfault.extensions.session import ExtensionContext, ExtensionSession


def _extension_handlers(importer, module, entry, action_info):
    handlers = {}
    for command in action_info.get("contributes", {}).get("commands", []):
        relative, symbol = command["handler"].rsplit(":", 1)
        path = (importer.root / relative).resolve()
        path.relative_to(importer.root)
        command_module = module if path == entry.resolve() else importer.load_file(str(path))
        handler = getattr(command_module, symbol, None)
        if not callable(handler):
            raise TypeError(f"扩展命令 {command['id']} 缺少函数 {symbol}")
        inspect.signature(handler).bind(object(), {})
        handlers[command["id"]] = handler
    return handlers


def _serve_extension(request, protocol, importer, module, entry, tree):
    handlers = _extension_handlers(importer, module, entry, request["action_info"])
    registry = ExtensionRegistry()
    session = ExtensionSession(**request["extension_session"])
    session.session_id = request["session_id"]
    session.type_registry = TypeRegistry(request.get("data_types"))
    registry.register_manifest(session.plugin_id, "action", session.plugin_meta, str(importer.root))
    context = ExtensionContext(session, registry)
    output_lock = threading.Lock()

    def send(payload):
        line = json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n"
        if len(line) > MAX_EXTENSION_PROTOCOL_LINE:
            raise ValueError("扩展 worker 响应超过大小限制")
        with output_lock:
            protocol.write(line)
            protocol.flush()

    set_status = session.set_status

    def update_status(text):
        set_status(text)
        send({"type": "status", "status": session.status})

    session.set_status = update_status

    def invoke(message):
        try:
            current_tree = inspect_plugin_tree(str(importer.root))
            if current_tree is None or current_tree.file_snapshot != tree.file_snapshot:
                raise PermissionError("隔离动作插件文件完整性校验失败")
            command_id = message["command"]
            if command_id not in session.allowed_commands:
                raise PermissionError("当前视图不能调用这个命令")
            result = session.invoke(handlers[command_id], context, message.get("payload", {}))
            send({
                "type": "result", "ok": True, "result": encode_value(result),
                "current_value": encode_value(session.current_value),
            })
        except BaseException as error:
            send({"type": "result", "ok": False, "error": f"{type(error).__name__}: {error}"})

    active = None
    try:
        send({"type": "result", "ok": True, "result": None})
        while True:
            line = sys.stdin.readline(MAX_EXTENSION_PROTOCOL_LINE + 1)
            if not line:
                break
            if len(line) > MAX_EXTENSION_PROTOCOL_LINE:
                raise ValueError("扩展 worker 请求超过大小限制")
            message = decode_value(json.loads(line))
            if message.get("close") is True:
                break
            active = threading.Thread(target=invoke, args=(message,), daemon=True)
            active.start()
    finally:
        session.close()
        if active is not None:
            active.join(timeout=5)
        importer.close()


def main() -> None:
    protocol = sys.stdout
    protocol.write(json.dumps({"type": "ready", "protocol": 1}) + "\n")
    protocol.flush()
    try:
        line = sys.stdin.readline(MAX_PROTOCOL_LINE + 1)
        if len(line) > MAX_PROTOCOL_LINE:
            sys.exit(2)
        request = json.loads(line)
    except (json.JSONDecodeError, OSError):
        sys.exit(2)

    sys.stdout = sys.stderr
    try:
        typed = request.get("value_encoding") == "typed-v1"
        if typed:
            request = decode_value(request, _transport=True)
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
        action_info = request["action_info"]
        importer = PluginImports(
            str(plugin_root), "isolated_action", tree.py_sources,
            resource_roots=(
                {action_info["id"]: str(plugin_root)} if action_info.get("id") else {}
            ),
        )
        module = importer.load_entry(str(entry))
        if "extension_session" in request:
            _serve_extension(request, protocol, importer, module, entry, tree)
            return
        elif request.get("load_only") is True:
            runner = module.run
            arguments = (action_info, {})
            if action_info.get("execution_api") == "context-v1":
                runner = getattr(module, "run_with_context", None)
                arguments = (action_info, {}, {})
            if not callable(runner):
                raise TypeError("隔离动作缺少可调用的执行入口")
            inspect.signature(runner).bind(*arguments)
            _extension_handlers(importer, module, entry, action_info)
            result = None
        else:
            context = request.get("context", {})
            if typed:
                context["_type_registry"] = TypeRegistry(request.get("data_types"))
            result = invoke_action(module.run, module, action_info, request["params"], context)
        if typed:
            result = encode_value(result, _transport=True)
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
