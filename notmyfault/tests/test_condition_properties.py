import copy
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st

from notmyfault.core.clock import FakeClock
from notmyfault.core.condition_program import build_matcher
from notmyfault.core.condition_runtime import ConditionRuntime
from notmyfault.tests.canvas_fixtures import event_rule
from notmyfault.tests.condition_reference import ReferenceConditionRuntime


signals = st.sampled_from(["a", "b", "c"])
leaf = st.builds(lambda kind, channel: {"type": kind, "params": {"channel": channel}}, signals, st.integers(0, 1))
absence = st.builds(lambda child, window, repeat: {"op": "not", "children": [child], "within_seconds": window, "repeat": repeat},
                    leaf, st.integers(1, 10), st.sampled_from(["once_until_seen", "every_window"]))


def groups(children):
    return st.one_of(
        st.builds(lambda items, pick: {"op": "any", "children": items, "pick": pick},
                  st.lists(children, min_size=1, max_size=3), st.sampled_from(["latest", "first"])),
        st.builds(lambda items, consume, ttl: {"op": "all", "children": items, "consume": consume, "ttl_seconds": ttl},
                  st.lists(children, min_size=1, max_size=3), st.sampled_from(["matched", "all", "none"]), st.integers(1, 20)),
        st.builds(lambda items, consume, window: {"op": "all", "children": items, "consume": consume, "within_seconds": window},
                  st.lists(children, min_size=1, max_size=3), st.sampled_from(["matched", "all", "none"]), st.integers(1, 10)),
    )


trees = st.recursive(st.one_of(leaf, absence), groups, max_leaves=8)
operations = st.lists(st.tuples(st.integers(0, 15), st.sampled_from(["poll", "a", "b", "c"]), st.integers(0, 1), st.booleans(), st.booleans(), st.booleans()),
                     min_size=1, max_size=80)


def identify(node):
    result = copy.deepcopy(node)
    events = []
    def collect(current):
        if 'op' not in current:
            events.append(current)
        for child in current.get('children', ()):
            collect(child)
    collect(result)
    for index, event in enumerate(events):
        event['binding_id'] = f't_leaf{index:06d}'
    return result


@settings(max_examples=350, deadline=None, database=None)
@given(trees, operations)
def test_heap_matcher_agrees_with_scanning_reference(node, actions):
    rule = event_rule('r_property001', '图匹配', identify(node), [])
    clock = FakeClock()
    runtime = ConditionRuntime(clock=clock)
    runtime.register_rule("r", rule)
    reference = ReferenceConditionRuntime(rule)
    def migrate():
        candidate = ConditionRuntime(clock=clock)
        candidate.configure({"r": SimpleNamespace(matcher_spec=build_matcher(copy.deepcopy(rule)))})
        candidate.migrate_from(runtime, {"r"})
        candidate.activate()
        return candidate

    for index, (delta, kind, channel, by_instance, timer_first, migrate_first) in enumerate(actions):
        clock.advance(delta)
        now = clock.monotonic()
        if index % 11 == 10 and migrate_first:
            runtime = migrate()
        if kind == "poll" or timer_first:
            expected = reference.poll(now)
            assert dict(runtime.take_due()).get("r") == expected
        if kind != "poll":
            payload = {"channel": channel, "value": index}
            instance = {"config": {"channel": channel}} if by_instance else None
            expected_due = reference.poll(now)
            expected_match = reference.match(kind, payload, now, instance)
            due, matches = runtime.process_event([("r", rule)], kind, payload, instance=instance)
            assert dict(due).get("r") == expected_due
            assert dict(matches).get("r") == expected_match
        if index % 11 == 10 and not migrate_first:
            runtime = migrate()


old_trees = st.recursive(
    st.one_of(leaf, st.builds(lambda child, window: {"op": "not", "children": [child], "within_seconds": window}, leaf, st.integers(1, 5))),
    lambda children: st.builds(lambda items: {"op": "all", "children": items}, st.lists(children, min_size=1, max_size=3)),
    max_leaves=8,
)


@settings(max_examples=250, deadline=None, database=None)
@given(old_trees, operations)
def test_all_and_absence_graphs_agree_with_scanning_reference(node, actions):
    rule = event_rule('r_property001', '图匹配', identify(node), [])
    clock = FakeClock()
    runtime = ConditionRuntime(clock=clock)
    previous = ReferenceConditionRuntime(rule)
    runtime.register_rule("r", rule)
    for index, (delta, kind, channel, by_instance, timer_first, migrate_first) in enumerate(actions):
        clock.advance(delta)
        assert dict(runtime.take_due()).get("r") == previous.poll(clock.monotonic())
        if kind != "poll":
            payload = {"channel": channel, "value": index}
            instance = {"config": {"channel": channel}} if by_instance else None
            assert runtime.match_and_take("r", rule, kind, payload, instance=instance) == previous.match(kind, payload, clock.monotonic(), instance)


@settings(max_examples=250, deadline=None, database=None)
@given(st.sampled_from(["any", "all"]), st.integers(1, 10), operations)
def test_any_and_window_graphs_agree_with_scanning_reference(op, window, actions):
    node = {"op": op, "children": [{"type": kind, "params": {"channel": 0}} for kind in ("a", "b", "c")]}
    if op == "all":
        node["within_seconds"] = window
    rule = event_rule('r_property001', '图匹配', identify(node), [])
    clock = FakeClock()
    runtime = ConditionRuntime(clock=clock)
    previous = ReferenceConditionRuntime(rule)
    runtime.register_rule("r", rule)
    for index, (delta, kind, channel, by_instance, timer_first, migrate_first) in enumerate(actions):
        clock.advance(delta)
        assert dict(runtime.take_due()).get("r") == previous.poll(clock.monotonic())
        if kind != "poll":
            payload = {"channel": channel, "value": index}
            instance = {"config": {"channel": channel}} if by_instance else None
            assert runtime.match_and_take("r", rule, kind, payload, instance=instance) == previous.match(kind, payload, clock.monotonic(), instance)
