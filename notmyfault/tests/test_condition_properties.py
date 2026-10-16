import copy
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st

from notmyfault.core.clock import FakeClock
from notmyfault.core.condition_program import build_matcher
from notmyfault.core.condition_runtime import ConditionRuntime
from notmyfault.tests.condition_reference import ReferenceConditionRuntime


signals = st.sampled_from(["a", "b", "c"])
operations = st.lists(st.tuples(st.integers(0, 15), st.sampled_from(["poll", "a", "b", "c"]), st.integers(0, 1), st.booleans(), st.booleans(), st.booleans()),
                     min_size=1, max_size=80)


@st.composite
def native_rules(draw, *, allowed_ops=("all", "any"), allow_repeat=True, max_leaves=8):
    count = draw(st.integers(1, max_leaves))
    nodes, edges, pool = [], [], []
    for index in range(1, count + 1):
        node = {'binding_id': f'a_signal_{index:06d}', 'type': 'trigger',
                'trigger': {'binding_id': f't_leaf{index:06d}', 'type': draw(signals),
                            'params': {'channel': draw(st.integers(0, 1))}}}
        if draw(st.booleans()):
            repeats = ["once_until_seen", "every_window"] if allow_repeat else ["once_until_seen"]
            node['occurrence'] = 'absent'
            node['duration_seconds'] = draw(st.integers(1, 10))
            node['repeat'] = draw(st.sampled_from(repeats))
        nodes.append(node)
        pool.append((node['binding_id'], 'event'))
    serial = 0
    while len(pool) > 1:
        size = draw(st.integers(2, min(3, len(pool))))
        start = draw(st.integers(0, len(pool) - size))
        segment = pool[start:start + size]
        serial += 1
        identity = f'a_logic_{serial:06d}'
        op = draw(st.sampled_from(allowed_ops))
        node = {'binding_id': identity, 'type': 'logic', 'op': op}
        if op == 'any':
            node['pick'] = draw(st.sampled_from(['latest', 'first']))
        else:
            node['consume'] = draw(st.sampled_from(['matched', 'all', 'none']))
            if draw(st.booleans()):
                node['within_seconds'] = draw(st.integers(1, 10))
            else:
                node['ttl_seconds'] = draw(st.integers(1, 20))
        nodes.append(node)
        for source, port in segment:
            edges.append({'source': source, 'port': port, 'target': identity, 'channel': 'condition'})
        pool[start:start + size] = [(identity, 'result')]
    root, root_port = pool[0]
    nodes.append({'binding_id': 'a_end_property01', 'type': 'end'})
    edges.append({'source': root, 'port': root_port, 'target': 'a_end_property01', 'channel': 'control'})
    return {'rule_id': 'r_property001', 'name': '图匹配',
            'workflow': {'entry': None, 'nodes': nodes, 'edges': edges}}


@settings(max_examples=350, deadline=None, database=None)
@given(native_rules(), operations)
def test_heap_matcher_agrees_with_scanning_reference(rule, actions):
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


@settings(max_examples=250, deadline=None, database=None)
@given(native_rules(allowed_ops=('all',), allow_repeat=False), operations)
def test_all_and_absence_graphs_agree_with_scanning_reference(rule, actions):
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
    nodes = [{'binding_id': f'a_signal_{index:06d}', 'type': 'trigger',
              'trigger': {'binding_id': f't_leaf{index:06d}', 'type': kind, 'params': {'channel': 0}}}
             for index, kind in enumerate(("a", "b", "c"), 1)]
    root = {'binding_id': 'a_logic_000001', 'type': 'logic', 'op': op}
    if op == "all":
        root["within_seconds"] = window
    nodes.append(root)
    edges = [{'source': node['binding_id'], 'port': 'event', 'target': root['binding_id'], 'channel': 'condition'} for node in nodes[:3]]
    nodes.append({'binding_id': 'a_end_property01', 'type': 'end'})
    edges.append({'source': root['binding_id'], 'port': 'result', 'target': 'a_end_property01', 'channel': 'control'})
    rule = {'rule_id': 'r_property001', 'name': '图匹配', 'workflow': {'entry': None, 'nodes': nodes, 'edges': edges}}
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
