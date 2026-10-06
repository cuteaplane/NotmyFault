from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import platform
import statistics
import subprocess
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psutil

from notmyfault.core.engine import AutomationEngine
from notmyfault.core.event_bus import EventBus
from notmyfault.core.plugin_worker import run_isolated_action
from notmyfault.core.runtime_rules import prepare_rules
from notmyfault.core.workflow import build_context
from notmyfault.security.security import SecurityMode


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "agent_files"


def metadata(identity, kind):
    meta = {
        "id": identity, "name": identity, "description": "性能测量插件",
        "enabled": True, "version_code": 1, "version": "1.0",
        "package_name": f"io.notmyfault.bench.{identity}", "params": [],
    }
    if kind == "trigger":
        meta.update(trigger_api="event-v2", outputs=[])
    return meta


def rules(count, trigger_count, steps=10):
    return [{
        "name": f"bench_{index}", "rule_id": f"r_bench{index:06}",
        "condition": {"type": f"signal_{index % trigger_count}", "params": {},
                      "binding_id": f"t_bench{index:06}"},
        "actions": [{"type": "noop", "params": {}, "binding_id": f"a_bench{step:06}"}
                    for step in range(steps)],
    } for index in range(count)]


def summary(samples):
    ordered = sorted(samples)
    return {
        "count": len(samples), "median_ms": statistics.median(samples) * 1000,
        "p99_ms": ordered[math.ceil(len(ordered) * .99) - 1] * 1000,
        "operations_per_second": len(samples) / sum(samples),
    }


def measure(operation, count):
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        operation()
        samples.append(time.perf_counter() - start)
    return summary(samples)


def make_engine(document, directory):
    store = SimpleNamespace(
        rules_path=str(directory / "rules.json"),
        plugin_manifest_path=str(directory / "plugin_manifest.json"),
        load_verified_rules=lambda: document,
    )
    engine = AutomationEngine({"rules": document}, rules_store=store)
    engine._alert_user = lambda *args, **kwargs: None
    engine.actions_meta["noop"] = metadata("noop", "action")
    engine.actions_funcs["noop"] = lambda meta, params: None
    for rule in document:
        identity = rule["condition"]["type"]
        engine.triggers_meta[identity] = metadata(identity, "trigger")
        engine.triggers_funcs[identity] = lambda meta, config, emit, stop: stop.wait()
    return engine


def dispatch(count):
    document = rules(500, 20, steps=0)
    triggers = {f"signal_{index}": metadata(f"signal_{index}", "trigger") for index in range(20)}
    snapshot = prepare_rules(document, triggers, {}, validate=False)
    matched = 0

    def submit(*args):
        nonlocal matched
        matched += 1

    bus = EventBus(
        snapshot_fn=lambda: snapshot, rules_lock=threading.RLock(),
        triggers_meta_fn=lambda: triggers, actions_meta_fn=lambda: {},
        is_shutdown_fn=lambda: False, safe_on_event=lambda *args: None,
        execute_workflow_cb=submit, scheduler_submit_fn=submit,
    )
    bus.emit_event("signal_0", {}, instance={"config": {}})
    result = measure(lambda: bus.emit_event("signal_0", {}, instance={"config": {}}), count)
    if matched != (count + 1) * 25:
        raise RuntimeError("事件分发数量不符")
    result.update(rules=500, trigger_types=20, matches_per_event=25,
                  events_per_second=result["operations_per_second"], budget_p99_ms=2,
                  budget_events_per_second=5000)
    return result


def workflow(directory, count, *, payload_mb=0):
    document = rules(1, 1)
    engine = make_engine(document, directory)
    if payload_mb:
        engine.actions_meta["noop"]["outputs"] = [{
            "name": "data", "type": "array", "label": "数据",
            "value_type": {"type": "array", "items": "text"},
        }]
        payload = [f"{index:05}" + "x" * 1019 for index in range(payload_mb * 1024)]
        engine.actions_funcs["noop"] = lambda meta, params: {"data": payload}
        document[0]["actions"] = document[0]["actions"][:1] + [
            {"type": "sink", "params": {}, "binding_id": f"a_sink{index:06}"}
            for index in range(10)
        ]
        engine.actions_meta["sink"] = metadata("sink", "action")
        engine.actions_funcs["sink"] = lambda meta, params: None
    rule = engine._get_runtime_snapshot().rules[0]
    downstream = []
    run_action = engine._workflow_executor.run_action

    def timed_action(action, *args, **kwargs):
        started = time.perf_counter()
        result = run_action(action, *args, **kwargs)
        if action["type"] == "sink":
            downstream.append(time.perf_counter() - started)
        return result

    engine._workflow_executor.run_action = timed_action

    def execute():
        context = build_context(rule["name"], "signal_0", {}, [], rule["rule_id"], "run_bench")
        engine._get_runtime_snapshot().prepare_context(rule, context)
        engine._workflow_executor.execute_workflow(rule["rule_id"], rule, rule["name"], context)
        if any(step["status"] != "ok" for step in context["steps"].values()):
            raise RuntimeError("性能测量中的步骤未成功")

    try:
        execute()
        downstream.clear()
        result = measure(execute, count)
        if payload_mb:
            result.update(payload_bytes=payload_mb * 1024 * 1024,
                          downstream_step=summary(downstream), target_downstream_scaling="constant")
            tracemalloc.start()
            try:
                execute()
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            result["peak_bytes"] = peak
        else:
            result["budget_per_step_ms"] = 1
        result.update(steps=len(rule["actions"]), per_step_ms=result["median_ms"] / len(rule["actions"]))
        return result
    finally:
        engine.shutdown(timeout=5)


def idle(directory, seconds):
    engine = make_engine(rules(100, 30, steps=1), directory)
    ready = threading.Event()
    shutdown = threading.Event()
    lock = threading.Lock()
    started = 0
    errors = []

    def trigger(meta, config, emit, stop):
        nonlocal started
        with lock:
            started += 1
            if started == 30:
                ready.set()
        stop.wait()

    def drive():
        try:
            engine.start(shutdown)
        except BaseException as error:
            errors.append(error)
            ready.set()

    for identity in engine.triggers_funcs:
        engine.triggers_funcs[identity] = trigger
    thread = threading.Thread(target=drive, name="BenchmarkEngine")
    with patch("notmyfault.platform.platform_support.show_notification", lambda *args: True):
        try:
            thread.start()
            if not ready.wait(5) or errors:
                raise RuntimeError(f"引擎未能进入空闲运行: {errors}")
            process = psutil.Process()
            before = process.cpu_times()
            start = time.perf_counter()
            shutdown.wait(seconds)
            elapsed = time.perf_counter() - start
            after = process.cpu_times()
            return {
                "rules": 100, "trigger_instances": len(engine._trigger_supervisor.health()),
                "threads": len(threading.enumerate()), "duration_seconds": elapsed,
                "cpu_percent": max(0, 100 * ((after.user - before.user) + (after.system - before.system)) / elapsed),
                "budget_cpu_percent": .5, "budget_threads": 10,
            }
        finally:
            shutdown.set()
            thread.join(5)
            engine.shutdown(timeout=5)
            if thread.is_alive() or errors:
                raise RuntimeError(f"引擎未能正常退出: {errors}")


def startup(directory, count):
    plugin_dir = directory / "plugins"
    for kind, total in (("action", 20), ("trigger", 20)):
        for index in range(total):
            identity = f"{kind}_{index}"
            path = plugin_dir / f"{kind}s" / identity
            path.mkdir(parents=True)
            (path / f"{kind}.json").write_text(json.dumps(metadata(identity, kind)), encoding="utf-8")
            source = "def run(meta, params):\n    return None\n" if kind == "action" else (
                "def run(meta, config, emit, stop):\n    stop.wait()\n"
            )
            (path / f"{kind}.py").write_text(source, encoding="utf-8")
    document = rules(100, 20, steps=1)
    for index, rule in enumerate(document):
        rule["condition"]["type"] = f"trigger_{index % 20}"
        rule["actions"][0]["type"] = f"action_{index % 20}"

    samples = []

    def ready():
        started = time.perf_counter()
        engine = make_engine([], directory)
        engine._security_mode = SecurityMode.PERMISSIVE
        engine._plugin_loader._security_mode = SecurityMode.PERMISSIVE
        try:
            engine.auto_load([(str(plugin_dir), "user")])
            snapshot = engine._prepare_rules(document)
            if snapshot.issues or len(snapshot) != 100:
                raise RuntimeError(f"启动规则准备失败: {snapshot.issues}")
            if engine._start_trigger_threads(snapshot) != 20:
                raise RuntimeError("启动触发器数量不符")
            samples.append(time.perf_counter() - started)
        finally:
            engine.shutdown(timeout=5)

    for _ in range(count):
        ready()
    result = summary(samples)
    result.update(rules=100, plugins=40, budget_ms=1500)
    return result


def reload_rules(directory, count):
    engine = make_engine(rules(100, 20, steps=1), directory)
    snapshot = engine._get_runtime_snapshot()
    engine._start_trigger_threads(snapshot)
    original = dict(engine._trigger_supervisor._threads)
    changed = rules(100, 20, steps=1)
    revision = 0

    def apply():
        nonlocal revision
        revision += 1
        changed[0]["name"] = f"修改后的名称_{revision}"
        candidate = engine._prepare_rules(changed)
        previous = engine._apply_hot_reload_rules(candidate)
        engine._start_trigger_threads(candidate)
        engine._accept_hot_reload_rules(previous)

    try:
        result = measure(apply, count)
        result.update(rules=100, changed_rules=1, budget_ms=100,
                      trigger_restarts=sum(engine._trigger_supervisor._threads.get(key) is not value
                                           for key, value in original.items()))
        return result
    finally:
        engine.shutdown(timeout=5)


def isolated(directory, count):
    path = directory / "worker" / "action.py"
    path.parent.mkdir()
    source = b"def run(meta, params):\n    return {'value': params['value']}\n"
    path.write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()

    def execute():
        result = run_isolated_action(str(path), digest, {}, {"value": 42}, {}, execute_timeout=10)
        if result != (True, {"value": 42}):
            raise RuntimeError("隔离动作结果不符")

    cold = measure(execute, 1)
    warm = measure(execute, count)
    warm.update(cold_ms=cold["median_ms"], budget_ms=20)
    return warm


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--output", type=Path, default=ARTIFACTS / "benchmarks.json")
    args = parser.parse_args()
    temp = ARTIFACTS / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bench-", dir=temp) as temporary:
        directory = Path(temporary)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            results = {
                "dispatch": dispatch(20 if args.quick else 1000),
                "workflow": workflow(directory, 5 if args.quick else 100),
                "large_payload": {
                    f"{size}_mb": workflow(directory, 3 if args.quick else 20, payload_mb=size)
                    for size in (1, 5, 10)
                },
                "idle": idle(directory, 1 if args.quick else 10),
                "startup": startup(directory, 1 if args.quick else 5),
                "reload": reload_rules(directory, 3 if args.quick else 20),
                "isolated": isolated(directory, 3 if args.quick else 100),
            }
    from notmyfault.version import __version__
    report = {"version": __version__, "python": platform.python_version(),
              "platform": platform.platform(), "quick": args.quick,
              "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "benchmarks": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
