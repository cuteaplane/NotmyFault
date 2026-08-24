import importlib.util
import json
import sys


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def main() -> None:
    try:
        request = json.loads(sys.stdin.readline())
    except (json.JSONDecodeError, OSError):
        sys.exit(2)

    protocol = sys.stdout
    sys.stdout = sys.stderr
    try:
        spec = importlib.util.spec_from_file_location(
            "isolated_action", request["entry"]
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        action_info = request["action_info"]
        if action_info.get("execution_api") == "context-v1":
            result = module.run_with_context(
                action_info,
                request["params"],
                request.get("context", {}),
            )
        else:
            result = module.run(action_info, request["params"])
        payload = {"ok": True, "result": _jsonable(result)}
    except BaseException as exc:  # 动作代码什么都能抛，包括 SystemExit
        import traceback

        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
        }
    sys.stdout = protocol
    protocol.write(json.dumps(payload, ensure_ascii=False) + "\n")
    protocol.flush()


if __name__ == "__main__":
    main()
