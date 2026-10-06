import json
import importlib.util
from pathlib import Path

import py7zr


def make_meta(plugin_kind: str, **overrides):
    meta = {
        "id": f"demo_{plugin_kind}",
        "name": "演示插件",
        "description": "演示用插件",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": f"com.test.demo_{plugin_kind}",
    }
    meta.update(overrides)
    return meta


def build_nmfp(tmp_path, meta, plugin_kind, tag="pkg", extra_files=None):
    json_name = "trigger.json" if plugin_kind == "triggers" else "action.json"
    source = tmp_path / f"source-{tag}" / f"plugin-{tag}"
    source.mkdir(parents=True)
    (source / json_name).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    entry = "trigger.py" if plugin_kind == "triggers" else "action.py"
    (source / entry).write_text(
        "def run(meta, params):\n    return {'ok': True}\n", encoding="utf-8"
    )
    for name, content in (extra_files or {}).items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    archive = tmp_path / f"plugin-{tag}.nmfp"
    with py7zr.SevenZipFile(archive, "w") as output:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                output.write(path, f"plugin-{tag}/{path.relative_to(source).as_posix()}")
    return archive


def post_archive(env, route, archive, data=None):
    with open(archive, "rb") as file:
        return env.client.post(
            route,
            headers=env.headers,
            data=data or {},
            files={"file": (archive.name, file, "application/octet-stream")},
        )


def load_plugin(ptype, name):
    filename = "action.py" if ptype == "actions" else "trigger.py"
    path = Path(__file__).resolve().parents[1] / ptype / name / filename
    spec = importlib.util.spec_from_file_location(f"scenario_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_hosted_samplers = []


def hosted_sampler(module, class_name, config, events, *, clock=None, meta=None, stopping=None):
    import threading
    from notmyfault.core.clock import SystemClock
    from notmyfault.core.compiled_rules import freeze, thaw
    from notmyfault.core.data_types import normalize_fields
    from notmyfault.core.rules import config_fingerprint
    from notmyfault.core.trigger_host import Instance
    from notmyfault.core.state_store import TriggerState
    from notmyfault.core.type_registry import TypeRegistry
    from notmyfault.plugin_api import TriggerContext, use_trigger_context
    from notmyfault.tests.api_support import create_test_engine

    if meta is None:
        meta = json.loads(Path(module.__file__).with_name("trigger.json").read_text())
    engine = create_test_engine({"rules": []})
    host = engine._trigger_host
    host.clock = clock or SystemClock()
    record = events if callable(events) else events.append
    host._dispatch = lambda _, payload, instance: record(thaw(payload))
    host._timer_loop = lambda: host._stop.wait()
    host.start()
    registry = TypeRegistry.from_plugins({meta["id"]: meta}, {})
    config = normalize_fields(config, meta.get("params"), registry, parameters=True)
    item = Instance((meta["id"], config_fingerprint(config)), freeze(meta), freeze(config), None, "test", registry,
                    TriggerState(engine._get_state_store, meta["id"]))
    if meta.get("state_key"):
        from notmyfault.core.trigger_state import StateDetector
        item.detector = StateDetector(meta, item.config, item.state)
    item.accepting = True
    item.generations = {1: 1}
    item.status = "running"
    item.thread = threading.Thread()
    host._instances[item.key] = item
    stop = stopping or threading.Event()

    def emit(payload):
        if stop.is_set():
            return False
        return host.emit(item, payload)

    ctx = TriggerContext(freeze(meta), freeze(config), emit, stop, item.state, host.boot_id, host.engine_started_at, host.clock)
    with use_trigger_context(ctx):
        trigger = getattr(module, class_name)(meta, config, emit, stop)
    poll = trigger.poll

    def sample():
        result = poll()
        host.service_timers()
        if not host.flush(3):
            raise TimeoutError("触发器测试事件未处理完")
        return result

    trigger.poll = sample
    trigger.host_clock = host.clock
    trigger.host = host
    trigger.test_engine = engine
    _hosted_samplers.append(engine)
    return trigger


def run_hosted_trigger(module, meta, config, emit, stopping):
    from notmyfault.triggers.base import PollingTrigger

    classes = [item for item in vars(module).values() if isinstance(item, type) and item is not PollingTrigger and issubclass(item, PollingTrigger)]
    if len(classes) != 1:
        raise ValueError("采样测试要求模块只有一个 PollingTrigger 子类")
    trigger = hosted_sampler(module, classes[0].__name__, config, emit, meta=meta, stopping=stopping)
    trigger.run()
    return trigger
