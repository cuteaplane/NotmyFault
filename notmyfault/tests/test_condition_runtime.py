import copy
import math
import queue
import threading
from types import SimpleNamespace

import pytest

from notmyfault.core.clock import FakeClock
from notmyfault.core.condition_program import build_matcher
from notmyfault.core.condition_runtime import ConditionRuntime, MatcherState
from notmyfault.core.canvas_workflow import validate_canvas_graph
from notmyfault.tests.canvas_fixtures import event_rule


def leaf(name):
    return {"type": name, "binding_id": f"t_{name}000000", "params": {}}


def absence_rule(identity, name, seconds):
    return {"rule_id": identity, "name": name, "workflow": {
        "entry": None,
        "nodes": [
            {"binding_id": "a_absence000001", "type": "trigger", "trigger": leaf("a"),
             "occurrence": "absent", "duration_seconds": seconds},
            {"binding_id": "a_finish000001", "type": "end"},
        ],
        "edges": [{"source": "a_absence000001", "port": "event", "target": "a_finish000001", "channel": "control"}],
    }}


def runtime_for(node):
    clock = FakeClock()
    runtime = ConditionRuntime(clock=clock)
    rule = event_rule('r_runtime001', '事件匹配', node, [])
    runtime.register_rule("r", rule)
    return runtime, clock, rule


def send(runtime, clock, rule, event, seconds=0):
    clock.advance(seconds)
    return runtime.match_and_take("r", rule, event, {"value": event})


@pytest.mark.parametrize("pick,chosen", [("latest", "b"), ("first", "a")])
@pytest.mark.parametrize("consume,second", [("matched", True), ("all", False)])
def test_any_selection_and_parent_consumption(pick, chosen, consume, second):
    runtime, clock, rule = runtime_for({"op": "all", "consume": consume, "children": [
        {"op": "any", "pick": pick, "children": [leaf("a"), leaf("b")]}, leaf("gate"),
    ]})
    assert send(runtime, clock, rule, "a") is None
    assert send(runtime, clock, rule, "b", 1) is None
    hits = send(runtime, clock, rule, "gate", 1)
    assert {entry["event"]["type"] for entry in hits} == {chosen, "gate"}
    assert (send(runtime, clock, rule, "gate", 1) is not None) == second


def test_none_retains_but_unchanged_combination_does_not_repeat():
    runtime, clock, rule = runtime_for({"op": "all", "consume": "none", "children": [leaf("a"), leaf("b")]})
    assert send(runtime, clock, rule, "a") is None
    assert send(runtime, clock, rule, "b") is not None
    assert runtime.take_due() == []
    assert send(runtime, clock, rule, "b") is not None
    assert len(runtime.snapshot()["r"].seen) == 2


def test_nested_none_retains_its_subtree_under_consuming_parent():
    runtime, clock, rule = runtime_for({"op": "all", "consume": "all", "children": [
        {"op": "all", "consume": "none", "children": [leaf("a"), leaf("b")]}, leaf("gate"),
    ]})
    send(runtime, clock, rule, "a")
    send(runtime, clock, rule, "b")
    assert send(runtime, clock, rule, "gate") is not None
    assert send(runtime, clock, rule, "gate") is not None
    assert len(runtime.snapshot()["r"].seen) == 2


@pytest.mark.parametrize("window", [None, 10])
def test_ttl_and_window_expire_without_new_events(window):
    node = {"op": "all", "children": [leaf("a"), leaf("b")]}
    if window is not None:
        node["within_seconds"] = window
    runtime, clock, rule = runtime_for(node)
    send(runtime, clock, rule, "a")
    clock.advance(3600 if window is None else math.nextafter(window, math.inf))
    assert runtime.take_due() == []
    assert not runtime.snapshot()["r"].seen
    assert runtime.next_deadline() is None
    assert send(runtime, clock, rule, "b") is None


def test_window_includes_exact_boundary():
    runtime, clock, rule = runtime_for({"op": "all", "within_seconds": 10, "children": [leaf("a"), leaf("b")]})
    send(runtime, clock, rule, "a")
    clock.advance(10)
    assert runtime.take_due() == []
    assert send(runtime, clock, rule, "b") is not None


def test_explicit_ttl_limits_retained_state_and_refreshes_on_new_event():
    runtime, clock, rule = runtime_for({"op": "all", "ttl_seconds": 2, "children": [leaf("a"), leaf("b")]})
    send(runtime, clock, rule, "a")
    send(runtime, clock, rule, "a", 1)
    clock.advance(1)
    runtime.take_due()
    assert len(runtime.snapshot()["r"].seen) == 1
    clock.advance(1)
    runtime.take_due()
    assert not runtime.snapshot()["r"].seen


@pytest.mark.parametrize("repeat,total", [("once_until_seen", 1), ("every_window", 3)])
def test_absence_repeat_and_rearm(repeat, total):
    runtime, clock, rule = runtime_for({"op": "not", "within_seconds": 2, "repeat": repeat, "children": [leaf("a")]})
    hits = []
    for _ in range(3):
        clock.advance(2)
        hits.extend(runtime.take_due())
    assert len(hits) == total
    assert runtime.take_due() == []
    send(runtime, clock, rule, "a")
    clock.advance(2)
    assert len(runtime.take_due()) == 1


def test_late_timer_coalesces_missed_absence_windows():
    runtime, clock, _rule = runtime_for({"op": "not", "within_seconds": 2, "repeat": "every_window", "children": [leaf("a")]})
    clock.advance(1000)
    assert len(runtime.take_due()) == 1
    assert runtime.next_deadline() == 1002


def test_tiny_absence_window_advances_beyond_clock_precision():
    clock = FakeClock(monotonic=1_000_000)
    runtime = ConditionRuntime(clock=clock)
    rule = absence_rule('r_runtime001', '事件匹配', 1e-300)
    rule['workflow']['nodes'][0]['repeat'] = 'every_window'
    runtime.register_rule("r", rule)
    assert runtime.next_deadline() > clock.monotonic()
    clock.advance(1)
    assert len(runtime.take_due()) == 1
    assert runtime.next_deadline() > clock.monotonic()


def test_heap_discards_stale_deadlines_without_growing_per_event():
    runtime, clock, rule = runtime_for({"op": "all", "children": [leaf("a"), leaf("b")]})
    for _ in range(1000):
        send(runtime, clock, rule, "a")
    assert len(runtime._heap) <= runtime._heap_limit
    assert len(runtime.snapshot()["r"].seen) == 1
    send(runtime, clock, rule, "b")
    assert runtime.next_deadline() is None


@pytest.mark.parametrize("consumed", [False, True])
def test_migration_keeps_deadlines_and_event_sequence(consumed):
    runtime, clock, rule = runtime_for({"op": "all", "consume": "matched", "children": [leaf("a"), leaf("b")]})
    send(runtime, clock, rule, "a")
    if consumed:
        send(runtime, clock, rule, "b")
    candidate = ConditionRuntime(clock=clock)
    candidate.configure({"r": SimpleNamespace(matcher_spec=build_matcher(copy.deepcopy(rule)))})
    candidate.migrate_from(runtime, {"r"})
    candidate.activate()
    state = candidate.snapshot()["r"]
    assert isinstance(state, MatcherState)
    assert candidate.next_deadline() == runtime.next_deadline()
    if consumed:
        send(candidate, clock, rule, "a")
    assert send(candidate, clock, rule, "b") is not None


def test_migration_preserves_tie_order_of_rearmed_absences():
    runtime, clock, rule = runtime_for({"op": "any", "children": [
        {"op": "not", "within_seconds": 1, "children": [leaf("b")]},
        {"op": "not", "within_seconds": 2, "children": [leaf("a")]},
    ]})
    send(runtime, clock, rule, "b", 1)
    candidate = ConditionRuntime(clock=clock)
    candidate.configure({"r": SimpleNamespace(matcher_spec=build_matcher(copy.deepcopy(rule)))})
    candidate.migrate_from(runtime, {"r"})
    candidate.activate()
    clock.advance(1)
    assert dict(candidate.take_due())["r"][0]["payload"]["wait_seconds"] == 1


@pytest.mark.parametrize("op,option,value", [
    ("all", "consume", "invalid"), ("any", "pick", []), ("not", "repeat", {}),
    ("any", "consume", "none"), ("not", "pick", "latest"),
    ("all", "ttl_seconds", 0), ("all", "ttl_seconds", True),
    ("all", "ttl_seconds", float("inf")), ("all", "ttl_seconds", 10**1000),
])
def test_invalid_options_are_reported(op, option, value):
    rule = event_rule('r_runtime001', '事件匹配', {'op': op, 'within_seconds': 2, 'children': [leaf('a')]}, [])
    node = rule['workflow']['nodes'][0]
    node[option] = value
    if op == "not":
        node['duration_seconds'] = 2
    assert any(option in error for error in validate_canvas_graph(rule['workflow']))


def test_timer_thread_waits_and_reload_wakes_it_for_an_earlier_deadline():
    from notmyfault.tests.api_support import create_test_engine

    class ObservedClock(FakeClock):
        def __init__(self):
            super().__init__()
            self.waits = queue.Queue()

        def wait(self, event, timeout):
            self.waits.put(timeout)
            return super().wait(event, timeout)

    rule = absence_rule("r_timertrial", "缺席计时", 10)
    finished = threading.Event()
    clock = ObservedClock()
    engine = create_test_engine({"rules": [rule]}, clock=clock, on_event=lambda event: finished.set() if event.type == "run.succeeded" else None)
    engine.triggers_meta["a"] = {"trigger_api": "event-v2"}
    engine._get_runtime_snapshot()
    engine._trigger_host.start()
    try:
        assert clock.waits.get(timeout=2) == 10
        changed = copy.deepcopy(rule)
        changed["workflow"]["nodes"][0]["duration_seconds"] = 3
        engine._generations.apply([changed], "test", activate=False, validate=False)
        assert clock.waits.get(timeout=2) == 3
        assert not finished.is_set()
        clock.advance(3)
        assert finished.wait(2)
        assert clock.waits.get(timeout=2) is None
    finally:
        engine.shutdown(timeout=3)
    assert not engine._trigger_host._timer.is_alive()


@pytest.mark.parametrize("timer_first", [False, True])
@pytest.mark.parametrize("repeat", ["once_until_seen", "every_window"])
@pytest.mark.parametrize("arrival", [2, 3, 7])
def test_event_ingress_delivers_due_absence_before_rearming(timer_first, repeat, arrival):
    runtime, clock, rule = runtime_for({"op": "not", "within_seconds": 2, "repeat": repeat, "children": [leaf("a")]})
    clock.advance(arrival)
    deliveries = runtime.take_due() if timer_first else []
    due, matches = runtime.process_event([("r", rule)], "a", {})
    deliveries.extend(due)
    assert len(deliveries) == 1
    assert matches == []
    assert runtime.next_deadline() == arrival + 2
    assert runtime.take_due() == []


@pytest.mark.parametrize("timer_first", [False, True])
def test_engine_event_ingress_executes_absence_when_timer_thread_is_late(timer_first):
    from notmyfault.tests.api_support import create_test_engine

    rule = absence_rule("r_latenot1", "缺席等待", 2)
    events = []
    engine = create_test_engine({"rules": [rule]}, on_event=events.append)
    engine.triggers_meta["a"] = {}
    clock = FakeClock()
    runtime = engine._get_runtime_snapshot().runtime
    runtime._clock = clock
    runtime.activate()
    try:
        clock.advance(3)
        if timer_first:
            engine._event_bus.process_timers()
        engine.emit_event("a", {})
        assert engine._run_manager.wait_for_idle(2)
        assert engine.events.flush()
        assert len([event for event in events if event.type == "run.succeeded"]) == 1
        assert runtime.next_deadline() == 5
    finally:
        engine.shutdown(2)
