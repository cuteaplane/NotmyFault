import hashlib
import json
import sys
import types
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


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


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
        entry = Path(request["entry"])
        source = entry.read_bytes()
        expected_hash = request.get("entry_sha256")
        if (
            not isinstance(expected_hash, str)
            or hashlib.sha256(source).hexdigest() != expected_hash
        ):
            raise PermissionError("隔离动作入口完整性校验失败")
        # 将插件根目录加入搜索路径，允许导入兄弟模块
        plugin_root = str(entry.resolve().parent)
        if plugin_root not in sys.path:
            sys.path.insert(0, plugin_root)
        module = types.ModuleType("isolated_action")
        module.__file__ = str(entry)
        exec(compile(source, str(entry), "exec"), module.__dict__)
        action_info = request["action_info"]
        if action_info.get("execution_api") == "context-v1":
            result = module.run_with_context(
                action_info,
                request["params"],
                request.get("context", {}),
            )
        else:
            result = module.run(action_info, request["params"])
        payload = {"type": "result", "ok": True, "result": _jsonable(result)}
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
