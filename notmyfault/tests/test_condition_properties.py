import copy
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st

from notmyfault.core.clock import FakeClock
from notmyfault.core.condition_program import build_matcher
from notmyfault.core.condition_runtime import ConditionRuntime
from notmyfault.core.conditions import iter_condition_events
from notmyfault.tests.condition_reference import ReferenceConditionRuntime
from notmyfault.tests.condition_runtime_oracle import PreviousConditionRuntime


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
    for index, event in enumerate(iter_condition_events(result)):
        event["binding_id"] = f"t_leaf{index:06d}"
    return result


@settings(max_examples=350, deadline=None, database=None)
@given(trees, operations)
def test_heap_matcher_agrees_with_scanning_reference(node, actions):
    rule = {"condition": identify(node)}
    clock = FakeClock()
    runtime = ConditionRuntime(clock=clock)
    runtime.register_rule("r", rule)
    reference = ReferenceConditionRuntime(rule)
    def migrate():
        candidate = ConditionRuntime(clock=clock)
        candidate.configure({"r": SimpleNamespace(matcher_spec=build_matcher(copy.deepcopy(rule["condition"])))})
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
def test_existing_all_and_absence_semantics_agree_with_previous_runtime(node, actions):
    rule = {"condition": identify(node)}
    clock = FakeClock()
    runtime = ConditionRuntime(clock=clock)
    previous = PreviousConditionRuntime(clock=clock)
    runtime.register_rule("r", rule)
    previous.poll_absences("r", rule)
    for index, (delta, kind, channel, by_instance, timer_first, migrate_first) in enumerate(actions):
        clock.advance(delta)
        assert dict(runtime.take_due()).get("r") == previous.poll_absences("r", rule)
        if kind != "poll":
            payload = {"channel": channel, "value": index}
            instance = {"config": {"channel": channel}} if by_instance else None
            assert runtime.match_and_take("r", rule, kind, payload, instance=instance) == previous.match_and_take("r", rule, kind, payload, instance=instance)
