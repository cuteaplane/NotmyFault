"""isolated action 的子进程入口

stdin 收一行 JSON 请求，stdout 只走协议，动作代码里的 print 全部落到 stderr，
这样动作乱写 stdout 也不会把协议线弄脏。退出码非 0 视为 worker 崩溃。
"""
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
        result = module.run(request["action_info"], request["params"])
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
