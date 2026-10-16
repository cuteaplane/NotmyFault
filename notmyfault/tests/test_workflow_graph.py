import copy
import threading
import time
from types import SimpleNamespace
from notmyfault.values import action_values

import pytest

from notmyfault.core.clock import FakeClock
from notmyfault.core.diagnostics import Diagnostics
from notmyfault.core.event_observation import RunEventObservation
from notmyfault.core.rules import normalize_rule_input
from notmyfault.core.binding_schema import check_rule_bindings, prepare_binding_context, output_type
from notmyfault.core.compiled_rules import compile_rule
from notmyfault.core.type_registry import TypeRegistry
from notmyfault.core.runtime_rules import prepare_rules
from notmyfault.core.run_manager import RunManager, RunSuspension
from notmyfault.core.workflow import build_context
from notmyfault.core.workflow_executor import WorkflowExecutor
from notmyfault.core.canvas_workflow import validate_canvas_graph
from notmyfault.core.workflow_graph import graph_index


TRIGGERS = {name: {'params': [], 'outputs': [{'name': 'value', 'type': 'number'}]} for name in ('start', 'steam', 'network')}
ACTIONS = {
    'record': {'params': [{'name': 'label', 'type': 'text'}], 'outputs': []},
    'speed': {'params': [], 'outputs': [{'name': 'latency', 'type': 'number'}, {'name': 'connected', 'type': 'bool'}]},
}


def leaf(kind='start', identity='t_start01'):
    return {'type': kind, 'binding_id': identity, 'params': {}}


def action(identity, label):
    return {'type': 'record', 'binding_id': identity, 'params': {'label': label}}


def edge(source, target, port='next', *, channel='control', target_port=None):
    result = {'source': source, 'target': target, 'port': port, 'channel': channel}
    if target_port is not None:
        result['target_port'] = target_port
    return result


def branch(identity, *, otherwise=False):
    return {'id': identity, **({'else': True} if otherwise else {})}


def group(identity, branches, **fields):
    return {'type': 'event_group', 'binding_id': identity, 'group_type': 'if',
            'mode': 'immediate', 'branches': branches, **fields}


def rule(nodes, edges):
    return {'rule_id': 'r_graph001', 'name': '图执行',
            'workflow': {'entry': None, 'nodes': nodes, 'edges': edges}}


def validate_runtime_structure(source):
    return validate_canvas_graph(source['workflow'])


def validate_runtime_bindings(source, triggers, actions):
    return check_rule_bindings(source, triggers, actions)


def trigger(identity, kind='start', parent=None, params=None):
    node = {'type': 'trigger', 'binding_id': identity, 'trigger': {
        'type': kind, 'binding_id': {'start': 't_start01', 'steam': 't_steam01',
                                     'network': 't_net001'}[kind],
        'params': params or {},
    }}
    if parent is not None:
        node['parent'] = parent
    return node


def setup(rule, clock=None, record=None):
    records, events = [], []
    source = rule
    registry = TypeRegistry.from_plugins(TRIGGERS, ACTIONS)
    context = build_context(source['name'], 'start', {}, [], source['rule_id'], 'run_graph')
    prepare_binding_context(source, context, TRIGGERS, ACTIONS, registry=registry)
    compiled = compile_rule(source, context, TRIGGERS, ACTIONS,
                            {name: output_type(meta) for name, meta in TRIGGERS.items()}, registry)
    source = compiled.source
    observer = RunEventObservation(compiled, clock=clock)
    observer.record('start', {})
    context['_compiled_rule'] = compiled
    context['_compiled_refs'] = compiled.references
    context['_event_observation'] = observer
    functions = {'record': lambda meta, params, context: (record(meta, params) if record else records.append(params['label'])),
                 'speed': lambda meta, params, context: {'latency': 180000, 'connected': True}}
    modules = {name: SimpleNamespace(run_with_context=lambda meta, params, context, name=name: functions[name](meta, params, context)) for name in functions}
    executor = WorkflowExecutor(actions_meta=lambda: ACTIONS, triggers_meta=lambda: TRIGGERS,
        actions_funcs=lambda: functions, plugin_modules=lambda: modules, diagnostics=Diagnostics(),
        shutdown_event=lambda: None, on_event=lambda kind, data: events.append((kind, data)), clock=clock)
    return source, context, observer, executor, records, events




def eventually(test, timeout=3):
    deadline = time.monotonic() + timeout
    while not test() and time.monotonic() < deadline:
        threading.Event().wait(.005)
    assert test()


def test_conditions_follow_edges_and_short_circuit_in_input_order():
    from notmyfault.core.canvas_workflow import CanvasProgram
    source = {'workflow': {'entry': None, 'nodes': [
        {'binding_id': 'a_true0001', 'type': 'condition', 'predicate': {'op': 'is_true', 'left': True}},
        {'binding_id': 'a_missing1', 'type': 'condition', 'predicate': {'op': 'eq', 'left': {'$ref': {'scope': 'step', 'node': 'a_missing', 'path': []}}, 'right': 1}},
        {'binding_id': 'a_choice01', 'type': 'logic', 'op': 'any'},
    ], 'edges': [
        {'source': identity, 'port': 'result', 'target': 'a_choice01', 'channel': 'condition'}
        for identity in ('a_true0001', 'a_missing1')
    ]}}
    assert CanvasProgram(source).evaluate('a_choice01', {})
    source['workflow']['edges'].reverse()
    with pytest.raises(ValueError):
        CanvasProgram(source).evaluate('a_choice01', {})
    source['workflow']['edges'].append({'source': 'a_choice01', 'port': 'result', 'target': 'a_choice01', 'channel': 'condition'})
    assert any('环' in error for error in validate_canvas_graph(source['workflow']))


def test_control_connections_override_node_array_order_and_merge_once():

    decision = group('a_decide01', [
        branch('b_a_decide01_0'), branch('b_a_decide01_1'), branch('b_a_decide01_2', otherwise=True)])
    nodes = [trigger('a_watch_start01'), action('a_else001', 'else'), action('a_after01', 'after'), decision,
             {'type': 'condition', 'binding_id': 'a_predicate_000001', 'parent': 'a_decide01',
              'predicate': {'op': 'is_true', 'left': True}},
             {'type': 'condition', 'binding_id': 'a_predicate_000002', 'parent': 'a_decide01',
              'predicate': {'op': 'is_true', 'left': True}},
             action('a_elif001', 'elif'), action('a_first01', 'first')]
    graph = rule(nodes, [edge('a_watch_start01', 'a_decide01', 'event'),
        edge('a_predicate_000001', 'a_decide01', 'result', channel='condition', target_port=decision['branches'][0]['id']),
        edge('a_predicate_000002', 'a_decide01', 'result', channel='condition', target_port=decision['branches'][1]['id']),
        edge('a_decide01', 'a_first01', decision['branches'][0]['id']),
        edge('a_decide01', 'a_elif001', decision['branches'][1]['id']),
        edge('a_decide01', 'a_else001', decision['branches'][2]['id']),
        edge('a_first01', 'a_after01'), edge('a_elif001', 'a_after01'), edge('a_else001', 'a_after01')])
    source, context, observer, executor, records, events = setup(graph)
    assert executor.execute_workflow('key', source, source['name'], context).status == 'succeeded'
    assert records == ['first', 'after']
    assert context['steps']['a_decide01']['result']['branch'] == decision['branches'][0]['id']
    assert sum(kind == 'run.group.selected' and data['step_id'] == 'a_decide01' for kind, data in events) == 1


def test_old_rule_conversion_is_rejected_without_changing_binding_ids():
    old = {'rule_id': 'r_graph001', 'name': '转换', 'condition': leaf(), 'actions': [
        {'type': 'if', 'binding_id': 'a_decide01', 'condition': {'op': 'is_true', 'left': False},
         'then': [action('a_first01', 'first')], 'elif': [{'condition': {'op': 'is_true', 'left': True}, 'then': [action('a_second1', 'second')]}], 'else': []},
        action('a_after01', 'after'),
    ]}
    original = copy.deepcopy(old)
    with pytest.raises(ValueError, match='旧条件模型|不支持|字段'):
        normalize_rule_input([old])
    assert old == original


def test_nested_groups_export_output_and_reject_direct_inner_reference():

    producer = {'type': 'speed', 'binding_id': 'a_speed01', 'parent': 'a_inner01', 'params': {}}
    inner = {'type': 'action_group', 'binding_id': 'a_inner01', 'parent': 'a_outer01',
             'exports': {'latency': {'$ref': {'scope': 'step', 'node': 'a_speed01', 'path': ['latency']}}}}
    outer = {'type': 'action_group', 'binding_id': 'a_outer01',
             'exports': {'latency': {'$ref': {'scope': 'step', 'node': 'a_inner01', 'path': ['latency']}}}}
    predicate = {'op': 'gt', 'left': {'$ref': {'scope': 'step', 'node': 'a_outer01', 'path': ['latency']}}, 'right': 80000}
    decision = group('a_decide01', [branch('b_a_decide01_0'), branch('b_a_decide01_1', otherwise=True)])
    condition = {'type': 'condition', 'binding_id': 'a_predicate_000001', 'parent': 'a_decide01', 'predicate': predicate}
    graph = rule([trigger('a_watch_start01'), outer, inner, producer, decision, condition, action('a_first01', 'pause')], [
        edge('a_watch_start01', 'a_outer01', 'event'), edge('a_outer01', 'a_inner01', 'entry'),
        edge('a_inner01', 'a_speed01', 'entry'), edge('a_outer01', 'a_decide01'),
        edge('a_predicate_000001', 'a_decide01', 'result', channel='condition', target_port='b_a_decide01_0'),
        edge('a_decide01', 'a_first01', 'b_a_decide01_0')])
    source, context, observer, executor, records, events = setup(graph)
    assert executor.execute_workflow('key', source, source['name'], context).status == 'succeeded'
    assert records == ['pause'] and context['steps']['a_outer01']['result']['latency'] == 180000
    condition['predicate'] = {**predicate, 'left': {'$ref': {'scope': 'step', 'node': 'a_speed01', 'path': ['latency']}}}
    assert any(issue['code'] == 'forward_reference' for issue in validate_runtime_bindings(graph, TRIGGERS, ACTIONS))


def waiting_rule(timeout=None):
    wait = group('a_wait001', [branch('b_a_wait001_0'), branch('b_a_wait001_1', otherwise=True)],
                 mode='wait', **({'timeout_seconds': timeout} if timeout is not None else {}))
    nodes = [trigger('a_watch_start01'), action('a_before1', 'before'), wait,
             trigger('a_watch_steam01', 'steam', parent='a_wait001'),
             action('a_after01', 'after')]
    return rule(nodes, [
        edge('a_watch_start01', 'a_before1', 'event'),
        edge('a_before1', 'a_wait001'),
        edge('a_wait001', 'a_after01', 'b_a_wait001_0'),
        edge('a_wait001', 'a_after01', 'b_a_wait001_1'),
        edge('a_watch_steam01', 'a_wait001', 'event', channel='condition',
             target_port='b_a_wait001_0'),
    ])


def test_wait_releases_single_worker_and_late_event_resumes_without_repeating_action():
    source, context, observer, executor, records, events = setup(waiting_rule())
    manager = RunManager(executor.execute_workflow, max_workers=1)
    try:
        assert manager.submit('one', source, source['name'], context) == 'started'
        eventually(lambda: any(kind == 'run.group.waiting' for kind, data in events))
        other = rule([trigger('a_watch_start01'), action('a_extra01', 'other')],
                     [edge('a_watch_start01', 'a_extra01', 'event')])
        other_source, other_context, _, _, _, _ = setup(other)
        other_context['run']['id'] = 'run_other'
        assert manager.submit('two', other_source, '另一条', other_context) == 'started'
        eventually(lambda: 'other' in records)
        observer.record('network', {'value': 1})
        assert manager.state('run_graph') == 'running' and records == ['before', 'other']
        observer.record('steam', {'value': 9})
        assert manager.wait_for_idle(3)
        assert records == ['before', 'other', 'after']
        assert context['triggers']['t_steam01']['payload']['value'] == 9
        assert manager.state('run_graph') == 'succeeded'
    finally:
        manager.shutdown()


@pytest.mark.parametrize('finish', ['cancel', 'timeout', 'shutdown', 'replace'])
def test_wait_ends_on_cancel_timeout_shutdown_or_replacement(finish):
    clock = FakeClock()
    source, context, observer, executor, records, events = setup(waiting_rule(timeout=2), clock)
    if finish == 'replace':
        from notmyfault.core.compiled_rules import thaw
        source = thaw(source)
        source['concurrency'] = {'mode': 'replace'}
    manager = RunManager(executor.execute_workflow, max_workers=1)
    try:
        manager.submit('one', source, source['name'], context)
        eventually(lambda: any(kind == 'run.group.waiting' for kind, data in events))
        if finish == 'timeout':
            clock.advance(2)
            observer.process_timers()
            expected = 'timed_out'
        elif finish == 'shutdown':
            manager.shutdown()
            expected = 'cancelled'
        elif finish == 'replace':
            other = rule([trigger('a_watch_start01'), action('a_extra01', 'new')],
                         [edge('a_watch_start01', 'a_extra01', 'event')])
            other['concurrency'] = {'mode': 'replace'}
            other_source, other_context, _, _, _, _ = setup(other)
            other_context['run']['id'] = 'run_other'
            manager.submit('one', other_source, '替换', other_context)
            expected = 'cancelled'
        else:
            assert manager.cancel_run('run_graph')
            expected = 'cancelled'
        assert manager.wait_for_idle(3)
        assert manager.state('run_graph') == expected
        assert 'after' not in records
    finally:
        manager.shutdown()


def test_event_arriving_between_yield_and_arm_is_not_lost():
    source, context, observer, executor, records, events = setup(waiting_rule())
    suspension = executor.execute_workflow('one', source, source['name'], context)
    assert isinstance(suspension, RunSuspension)
    observer.record('steam', {'value': 1})
    woke = threading.Event()
    suspension.wait.arm(woke.set)
    assert woke.is_set()
    assert executor.execute_workflow('one', source, source['name'], context).status == 'succeeded'
    assert records == ['before', 'after']


def test_wait_window_expires_old_event_and_timed_absence_is_distinct_from_boolean_not():
    clock = FakeClock()
    source = waiting_rule()
    graph = source['workflow']
    graph['nodes'].append({'type': 'logic', 'binding_id': 'a_predicate_000001', 'parent': 'a_wait001',
                           'op': 'all', 'within_seconds': 2})
    graph['nodes'].append(trigger('a_watch_net001', 'network', parent='a_wait001'))
    graph['edges'] = [connection for connection in graph['edges']
                      if connection['source'] != 'a_watch_steam01']
    graph['edges'].extend([
        edge('a_watch_steam01', 'a_predicate_000001', 'event', channel='condition'),
        edge('a_watch_net001', 'a_predicate_000001', 'event', channel='condition'),
        edge('a_predicate_000001', 'a_wait001', 'result', channel='condition', target_port='b_a_wait001_0')])
    source, context, observer, executor, records, events = setup(source, clock)
    observer.record('steam', {'value': 1})
    clock.advance(3)
    observer.record('network', {'value': 1})
    first = executor.execute_workflow('one', source, source['name'], context)
    assert isinstance(first, RunSuspension)
    observer.record('steam', {'value': 2})
    assert first.wait.ready
    assert executor.execute_workflow('one', source, source['name'], context).status == 'succeeded'
    source = waiting_rule()
    graph = source['workflow']
    graph['nodes'].append({'type': 'logic', 'binding_id': 'a_predicate_000001', 'parent': 'a_wait001',
                           'op': 'not', 'duration_seconds': 2})
    graph['edges'] = [connection for connection in graph['edges']
                      if connection['source'] != 'a_watch_steam01']
    graph['edges'].extend([
        edge('a_watch_steam01', 'a_predicate_000001', 'event', channel='condition'),
        edge('a_predicate_000001', 'a_wait001', 'result', channel='condition', target_port='b_a_wait001_0')])
    source, context, observer, executor, records, events = setup(source, clock)
    suspension = executor.execute_workflow('one', source, source['name'], context)
    assert isinstance(suspension, RunSuspension)
    clock.advance(2)
    observer.process_timers()
    assert suspension.wait.ready
    assert executor.execute_workflow('one', source, source['name'], context).status == 'succeeded'
    assert 't_steam01' not in observer.snapshot()


@pytest.mark.parametrize('mutate, message', [
    (lambda graph: graph['edges'].append(edge('a_first01', 'a_first01')), '环'),
    (lambda graph: graph['edges'].append(edge('a_first01', 'a_second1')), '一个目标'),
    (lambda graph: graph['edges'].append(edge('a_second1', 'a_inner01')), '不存在'),
])
def test_graph_validation_rejects_cycle_fanout_and_missing_target(mutate, message):
    graph = rule([trigger('a_watch_start01'), action('a_first01', 'first'), action('a_second1', 'second')],
                 [edge('a_watch_start01', 'a_first01', 'event'), edge('a_first01', 'a_second1')])
    mutate(graph['workflow'])
    assert any(message in error for error in validate_runtime_structure(graph))


def test_control_edge_cannot_enter_an_action_group_member_directly():
    inside = action('a_inside01', 'inside') | {'parent': 'a_outer01'}
    outer = {'type': 'action_group', 'binding_id': 'a_outer01'}
    source = rule([trigger('a_watch_start01'), action('a_first01', 'first'), outer, inside], [
        edge('a_watch_start01', 'a_first01', 'event'), edge('a_first01', 'a_outer01'),
        edge('a_outer01', 'a_inside01', 'entry')])
    connection = next(item for item in source['workflow']['edges'] if item['source'] == 'a_first01')
    connection['target'] = 'a_inside01'
    assert any('跨越动作组边界' in error for error in validate_runtime_structure(source))


def test_unconnected_nodes_remain_editable_without_entering_execution():
    from notmyfault.core.workflow_graph import execution_nodes
    source = rule([trigger('a_watch_start01'), action('a_first01', 'first')],
                  [edge('a_watch_start01', 'a_first01', 'event')])
    source['workflow']['nodes'].append(action('a_orphan1', 'unused'))
    assert validate_runtime_structure(source) == []
    assert 'a_orphan1' not in {node['binding_id'] for node, _ in execution_nodes(source['workflow'])}


def test_two_waiting_runs_keep_independent_observation_and_single_mode_admission():
    source, first, first_observer, executor, records, events = setup(waiting_rule())
    _, second, second_observer, _, _, _ = setup(waiting_rule())
    second['run']['id'] = 'run_second'
    manager = RunManager(executor.execute_workflow, max_workers=1)
    try:
        manager.submit('one', source, source['name'], first)
        eventually(lambda: sum(kind == 'run.group.waiting' for kind, data in events) == 1)
        assert manager.submit('one', source, source['name'], second) == 'dropped'
        _, second, second_observer, _, _, _ = setup(waiting_rule())
        second['run']['id'] = 'run_second_other'
        manager.submit('two', source, source['name'], second)
        eventually(lambda: sum(kind == 'run.group.waiting' for kind, data in events) == 2)
        first_observer.record('steam', {'value': 1})
        eventually(lambda: manager.state('run_graph') == 'succeeded')
        assert manager.state('run_second_other') == 'running'
        second_observer.record('steam', {'value': 2})
        assert manager.wait_for_idle(3)
        assert records.count('before') == 2 and records.count('after') == 2
    finally:
        manager.shutdown()


def test_nested_wait_retains_group_path_and_continues_outer_edges():
    source = waiting_rule()
    graph = source['workflow']
    outer = {'type': 'action_group', 'binding_id': 'a_outer01'}
    for identity in ('a_before1', 'a_wait001', 'a_after01'):
        next(node for node in graph['nodes'] if node['binding_id'] == identity)['parent'] = 'a_outer01'
    graph['nodes'].append(outer)
    graph['nodes'].append(action('a_finish1', 'finish'))
    graph['edges'] = [connection for connection in graph['edges']
                      if not (connection['source'] == 'a_watch_start01' and connection['target'] == 'a_before1')]
    graph['edges'].extend([edge('a_watch_start01', 'a_outer01', 'event'),
                           edge('a_outer01', 'a_before1', 'entry'),
                           edge('a_outer01', 'a_finish1')])
    source, context, observer, executor, records, events = setup(source)
    manager = RunManager(executor.execute_workflow, max_workers=1)
    try:
        manager.submit('one', source, source['name'], context)
        eventually(lambda: any(kind == 'run.step.waiting' for kind, data in events))
        waiting = next(data for kind, data in events if kind == 'run.step.waiting')
        assert waiting['group_path'] == ['a_outer01']
        observer.record('steam', {'value': 1})
        assert manager.wait_for_idle(3)
        assert records == ['before', 'after', 'finish']
        assert next(data for kind, data in events if kind == 'run.step.started' and data['step_id'] == 'a_after01')['group_path'] == ['a_outer01']
        assert next(data for kind, data in events if kind == 'run.step.started' and data['step_id'] == 'a_finish1')['group_path'] == []
    finally:
        manager.shutdown()


def test_optional_branch_output_requires_missing_policy_and_type_is_checked():
    decision = group('a_decide01', [branch('b_a_decide01_0'), branch('b_a_decide01_1', otherwise=True)])
    condition = {'type': 'condition', 'binding_id': 'a_predicate_000001', 'parent': 'a_decide01',
                 'predicate': {'op': 'is_true', 'left': False}}
    producer = {'type': 'speed', 'binding_id': 'a_speed01', 'params': {}}
    ref = {'$ref': {'scope': 'step', 'node': 'a_speed01', 'path': ['latency']}}
    assignment = {'type': 'set_variable', 'binding_id': 'a_assign1', 'variable': 'v_result1', 'value': ref}
    graph = rule([trigger('a_watch_start01'), decision, condition, producer, assignment], [
        edge('a_watch_start01', 'a_decide01', 'event'),
        edge('a_predicate_000001', 'a_decide01', 'result', channel='condition', target_port='b_a_decide01_0'),
        edge('a_decide01', 'a_speed01', 'b_a_decide01_0'),
        edge('a_decide01', 'a_assign1', 'b_a_decide01_1'), edge('a_speed01', 'a_assign1')])
    graph['variables'] = [{'id': 'v_result1', 'name': '延迟', 'value_type': 'number', 'initial': 0}]
    assert any(issue['code'] == 'conditional_source' for issue in validate_runtime_bindings(graph, TRIGGERS, ACTIONS))
    ref['$ref'].update(on_missing='default', default=80000)
    assert validate_runtime_bindings(graph, TRIGGERS, ACTIONS) == []
    source, context, observer, executor, records, events = setup(graph)
    assert executor.execute_workflow('one', source, source['name'], context).status == 'succeeded'
    assert context['variables']['v_result1'] == 80000
    graph['variables'][0]['value_type'] = 'bool'
    graph['variables'][0]['initial'] = False
    assert any(issue['code'] == 'binding_type_mismatch' for issue in validate_runtime_bindings(graph, TRIGGERS, ACTIONS))


def test_entry_group_matches_start_only_and_middle_trigger_is_subscribed():
    from notmyfault.tests.api_support import create_test_engine
    decision = group('a_entry01', [branch('b_a_entry01_0'), branch('b_a_entry01_1', otherwise=True)])
    middle = group('a_decide01', [branch('b_a_decide01_0'), branch('b_a_decide01_1', otherwise=True)])
    start = trigger('a_watch_start01')
    steam = trigger('a_watch_steam01', 'steam')
    source = rule([start, steam, decision, middle, action('a_first01', 'first'), action('a_else001', 'else')], [
        edge('a_watch_start01', 'a_entry01', 'event'),
        edge('a_watch_start01', 'a_entry01', 'event', channel='condition', target_port='b_a_entry01_0'),
        edge('a_watch_steam01', 'a_decide01', 'event', channel='condition', target_port='b_a_decide01_0'),
        edge('a_entry01', 'a_decide01', 'b_a_entry01_0'), edge('a_entry01', 'a_else001', 'b_a_entry01_1'),
        edge('a_decide01', 'a_first01', 'b_a_decide01_0'), edge('a_decide01', 'a_else001', 'b_a_decide01_1')])
    calls = []
    engine = create_test_engine({'rules': [source]})
    engine.triggers_meta.update(TRIGGERS)
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params, context: calls.append(params['label'])
    try:
        snapshot = engine._get_runtime_snapshot()
        assert {entry.trigger_id for entry in snapshot.compiled['r_graph001'].subscriptions} == {'start', 'steam'}
        assert set(snapshot.event_index) == {'start'}
        engine.emit_event('steam', {'value': 1})
        assert engine._run_manager.wait_for_idle(3)
        assert calls == []
        engine.emit_event('start', {'value': 1})
        assert engine._run_manager.wait_for_idle(3)
        assert calls == ['else']
    finally:
        engine.shutdown(3)


def test_entry_route_is_fixed_before_queue_and_late_event_counts_only_middle_group():
    from notmyfault.tests.api_support import create_test_engine
    entered, release = threading.Event(), threading.Event()
    decision = group('a_entry01', [branch('b_a_entry01_0'), branch('b_start_elif01'),
                                   branch('b_a_entry01_1', otherwise=True)])
    middle = group('a_decide01', [branch('b_a_decide01_0'), branch('b_a_decide01_1', otherwise=True)])
    source = rule([trigger('a_watch_start01'), trigger('a_watch_steam01', 'steam'), decision, middle,
        action('a_first01', 'first'), action('a_else001', 'middle'), action('a_wrong01', 'wrong')], [
        edge('a_watch_steam01', 'a_entry01', 'event', channel='condition', target_port='b_a_entry01_0'),
        edge('a_watch_start01', 'a_entry01', 'event', channel='condition', target_port='b_start_elif01'),
        edge('a_watch_steam01', 'a_decide01', 'event', channel='condition', target_port='b_a_decide01_0'),
        edge('a_entry01', 'a_wrong01', 'b_a_entry01_0'),
        edge('a_entry01', 'a_decide01', 'b_start_elif01'),
        edge('a_decide01', 'a_else001', 'b_a_decide01_0'), edge('a_decide01', 'a_first01', 'b_a_decide01_1')])
    calls, packets = [], []
    engine = create_test_engine({'rules': [source]}, on_event=lambda event: packets.append(event.to_dict()))
    engine.triggers_meta.update(TRIGGERS)
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params, context: calls.append(params['label'])
    manager = RunManager(engine._workflow_executor.execute_workflow, max_workers=1)
    engine._run_manager = manager
    engine._event_bus._status_lookup = manager.rule_status
    engine.actions_funcs['block'] = lambda meta, params, context: (entered.set(), release.wait(3))
    engine.actions_meta['block'] = {'params': [], 'outputs': []}
    blocking = {'rule_id': 'r_block001', 'name': '占用', 'workflow': {'entry': None, 'nodes': [
        trigger('a_watch_start01'), {'type': 'block', 'binding_id': 'a_block01', 'params': {}}],
        'edges': [edge('a_watch_start01', 'a_block01', 'event')]}}
    try:
        block_context = build_context('占用', 'start', {}, [], 'r_block001', 'run_block')
        block_context['_entry_root'] = 'a_watch_start01'
        block_context['_entry_choices'] = {'a_watch_start01': 'event'}
        manager.submit('block', blocking, '占用执行线程', block_context)
        assert entered.wait(3)
        engine.emit_event('start', {'value': 1})
        eventually(lambda: manager.stats().get('r_graph001', {}).get('queued') == 1)
        engine.emit_event('steam', {'value': 1})
        release.set()
        assert manager.wait_for_idle(3)
        assert calls == ['middle']
        assert engine.events.flush()
        completed = {packet['step_id']: packet['data'] for packet in packets if packet['type'] == 'run.step.succeeded' and packet.get('step_id') in ('a_entry01', 'a_decide01')}
        assert completed['a_entry01']['evaluated_branches'][0]['result'] is False
        assert completed['a_decide01']['evaluated_branches'][0]['result'] is True
    finally:
        release.set()
        engine.shutdown(3)


def test_old_conversion_remains_rejected_on_repeated_calls():
    old = {'rule_id': 'r_graph001', 'name': '转换', 'condition': leaf(), 'actions': [
        {'type': 'if', 'binding_id': 'a_decide01', 'condition': {'op': 'is_true', 'left': True}, 'then': [action('a_first01', 'first')], 'else': []}]}
    for _ in range(2):
        with pytest.raises(ValueError, match='旧条件模型|不支持|字段'):
            normalize_rule_input([old])


def test_nested_entry_event_group_can_start_outer_action_groups():
    from notmyfault.tests.api_support import create_test_engine
    decision = group('a_entry01', [branch('b_a_entry01_0'), branch('b_a_entry01_1', otherwise=True)])
    inner = {'type': 'action_group', 'binding_id': 'a_inner01', 'parent': 'a_outer01'}
    outer = {'type': 'action_group', 'binding_id': 'a_outer01'}
    entry = decision | {'parent': 'a_inner01'}
    first = action('a_first01', 'first') | {'parent': 'a_inner01'}
    start = trigger('a_watch_start01', parent='a_entry01')
    after = action('a_after01', 'after')
    source = rule([outer, inner, entry, first, start, after], [
        edge('a_outer01', 'a_inner01', 'entry'), edge('a_inner01', 'a_entry01', 'entry'),
        edge('a_watch_start01', 'a_entry01', 'event', channel='condition', target_port='b_a_entry01_0'),
        edge('a_entry01', 'a_first01', 'b_a_entry01_0'), edge('a_outer01', 'a_after01', 'next')])
    from notmyfault.core.condition_program import build_matcher
    assert set(build_matcher(source).leaves) == {'start'}
    calls = []
    engine = create_test_engine({'rules': [source]})
    engine.triggers_meta.update(TRIGGERS)
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params, context: calls.append(params['label'])
    try:
        engine.emit_event('start', {'value': 1})
        assert engine._run_manager.wait_for_idle(3)
        assert calls == ['first', 'after']
    finally:
        engine.shutdown(3)


def _download_graph():
    ref = lambda identity, field: {'$ref': {'scope': 'step', 'node': identity, 'path': [field]}}
    limit = {'$ref': {'scope': 'variable', 'node': 'v_limit01', 'path': []}}
    def group_node(identity, branches, *, mode='immediate', parent=None):
        node = {'type': 'event_group', 'binding_id': identity, 'group_type': 'if',
                'mode': mode, 'branches': branches}
        if parent is not None:
            node['parent'] = parent
        return node
    def logic(identity, parent, op):
        return {'type': 'logic', 'binding_id': identity, 'parent': parent, 'op': op}
    def condition(identity, parent, predicate):
        return {'type': 'condition', 'binding_id': identity, 'parent': parent, 'predicate': predicate}
    def member(node, parent):
        return {**node, 'parent': parent}

    dispatch = group_node('a_dispatch', [
        branch('b_a_dispatch_0'), branch('b_a_dispatch_1'), branch('b_a_dispatch_2', otherwise=True)])
    needfix = group_node('a_needfix1', [branch('b_a_needfix1_0'), branch('b_a_needfix1_1', otherwise=True)],
                         parent='a_protect1')
    quality = group_node('a_quality1', [branch('b_a_quality1_0'), branch('b_a_quality1_1', otherwise=True)],
                         parent='a_repair01')
    recovery_wait = group_node('a_recover1', [
        branch('b_a_recover1_0'), branch('b_a_recover1_1'),
        branch('b_a_recover1_2', otherwise=True)], mode='wait')
    recover_safe = group_node('a_recoversafe', [
        branch('b_a_recoversafe_0'), branch('b_a_recoversafe_1', otherwise=True)], parent='a_recovergroup')
    direct_safe = group_node('a_direct1safe', [
        branch('b_a_direct1safe_0'), branch('b_a_direct1safe_1', otherwise=True)], parent='a_direct1group')
    nodes = [
        {'type': 'trigger', 'binding_id': 'a_watch_start01', 'trigger': leaf()},
        {'type': 'trigger', 'binding_id': 'a_watch_steam01', 'trigger': {**leaf('steam', 't_steam01'), 'params': {'value': 1}}},
        {'type': 'trigger', 'binding_id': 'a_watch_exit001', 'trigger': {**leaf('steam', 't_exit001'), 'params': {'value': 0}}},
        {'type': 'trigger', 'binding_id': 'a_watch_net001', 'trigger': {**leaf('network', 't_net001'), 'params': {'value': 1}}},
        {'type': 'speed', 'binding_id': 'a_measure1', 'params': {'phase': 'first'}},
        dispatch,
        {'type': 'action_group', 'binding_id': 'a_protect1'},
        member(action('a_capture1', 'capture'), 'a_protect1'),
        member({'type': 'set_variable', 'binding_id': 'a_pause001', 'variable': 'v_status1', 'value': 'paused'}, 'a_protect1'),
        needfix,
        member({'type': 'action_group', 'binding_id': 'a_repair01'}, 'a_protect1'),
        member(action('a_relink1', 'reconnect'), 'a_repair01'),
        member({'type': 'speed', 'binding_id': 'a_recheck1', 'params': {'phase': 'repair'}}, 'a_repair01'),
        quality,
        member(action('a_good001', 'repair_ok'), 'a_repair01'),
        member(action('a_bad0001', 'repair_failed'), 'a_repair01'),
        member(action('a_skipfix1', 'skip_repair'), 'a_protect1'),
        recovery_wait,
        {'type': 'action_group', 'binding_id': 'a_recovergroup'},
        {'type': 'speed', 'binding_id': 'a_recoverspeed', 'parent': 'a_recovergroup', 'params': {'phase': 'repair'}},
        recover_safe,
        member({'type': 'set_variable', 'binding_id': 'a_recoverset1', 'variable': 'v_status1', 'value': 'running'}, 'a_recovergroup'),
        member(action('a_recovernote1', 'resume'), 'a_recovergroup'),
        member({'type': 'set_variable', 'binding_id': 'a_recoverset0', 'variable': 'v_status1', 'value': 'paused'}, 'a_recovergroup'),
        member(action('a_recovernote0', 'keep_paused'), 'a_recovergroup'),
        {'type': 'action_group', 'binding_id': 'a_direct1group'},
        {'type': 'speed', 'binding_id': 'a_direct1speed', 'parent': 'a_direct1group', 'params': {'phase': 'repair'}},
        direct_safe,
        member({'type': 'set_variable', 'binding_id': 'a_direct1set1', 'variable': 'v_status1', 'value': 'running'}, 'a_direct1group'),
        member(action('a_direct1note1', 'resume'), 'a_direct1group'),
        member({'type': 'set_variable', 'binding_id': 'a_direct1set0', 'variable': 'v_status1', 'value': 'paused'}, 'a_direct1group'),
        member(action('a_direct1note0', 'keep_paused'), 'a_direct1group'),
        action('a_hold001', 'hold_paused'), action('a_unknown1', 'uncertain'), action('a_idle001', 'idle'),
        logic('a_predicate_000001', 'a_dispatch', 'any'),
        logic('a_predicate_000002', 'a_dispatch', 'all'),
        condition('a_predicate_000003', 'a_dispatch', {'op': 'run_status', 'rule_id': 'r_graph001', 'status': 'running'}),
        logic('a_predicate_000004', 'a_dispatch', 'not'),
        condition('a_predicate_000005', 'a_dispatch', {'op': 'lte', 'left': ref('a_measure1', 'latency'), 'right': limit}),
        condition('a_predicate_000006', 'a_dispatch', {'op': 'is_false', 'left': ref('a_measure1', 'connected')}),
        logic('a_predicate_000007', 'a_dispatch', 'all'),
        logic('a_predicate_000008', 'a_dispatch', 'any'),
        condition('a_predicate_000009', 'a_dispatch', {'op': 'is_true', 'left': ref('a_measure1', 'connected')}),
        condition('a_predicate_000010', 'a_needfix1', {'op': 'is_false', 'left': ref('a_measure1', 'connected')}),
        logic('a_predicate_000011', 'a_quality1', 'all'),
        condition('a_predicate_000012', 'a_quality1', {'op': 'is_true', 'left': ref('a_recheck1', 'connected')}),
        logic('a_predicate_000013', 'a_quality1', 'any'),
        condition('a_predicate_000014', 'a_quality1', {'op': 'lte', 'left': ref('a_recheck1', 'latency'), 'right': limit}),
        condition('a_predicate_000015', 'a_quality1', {'op': 'lt', 'left': ref('a_recheck1', 'latency'), 'right': ref('a_measure1', 'latency')}),
        logic('a_predicate_000016', 'a_recover1', 'all'),
        logic('a_predicate_000017', 'a_recover1', 'not'),
        logic('a_predicate_000018', 'a_recoversafe', 'all'),
        condition('a_predicate_000019', 'a_recoversafe', {'op': 'is_true', 'left': ref('a_recoverspeed', 'connected')}),
        condition('a_predicate_000020', 'a_recoversafe', {'op': 'lte', 'left': ref('a_recoverspeed', 'latency'), 'right': limit}),
        logic('a_predicate_000021', 'a_direct1safe', 'all'),
        condition('a_predicate_000022', 'a_direct1safe', {'op': 'is_true', 'left': ref('a_direct1speed', 'connected')}),
        condition('a_predicate_000023', 'a_direct1safe', {'op': 'lte', 'left': ref('a_direct1speed', 'latency'), 'right': limit}),
    ]
    edges = [
        edge('a_watch_start01', 'a_measure1', 'event'), edge('a_measure1', 'a_dispatch'),
        edge('a_watch_steam01', 'a_predicate_000002', 'event', channel='condition'),
        edge('a_predicate_000003', 'a_predicate_000002', 'result', channel='condition'),
        edge('a_predicate_000005', 'a_predicate_000004', 'result', channel='condition'),
        edge('a_predicate_000004', 'a_predicate_000002', 'result', channel='condition'),
        edge('a_predicate_000002', 'a_predicate_000001', 'result', channel='condition'),
        edge('a_predicate_000006', 'a_predicate_000001', 'result', channel='condition'),
        edge('a_predicate_000001', 'a_dispatch', 'result', channel='condition', target_port='b_a_dispatch_0'),
        edge('a_watch_exit001', 'a_predicate_000008', 'event', channel='condition'),
        edge('a_watch_net001', 'a_predicate_000008', 'event', channel='condition'),
        edge('a_predicate_000008', 'a_predicate_000007', 'result', channel='condition'),
        edge('a_predicate_000009', 'a_predicate_000007', 'result', channel='condition'),
        edge('a_predicate_000007', 'a_dispatch', 'result', channel='condition', target_port='b_a_dispatch_1'),
        edge('a_dispatch', 'a_protect1', 'b_a_dispatch_0'),
        edge('a_dispatch', 'a_direct1group', 'b_a_dispatch_1'),
        edge('a_dispatch', 'a_idle001', 'b_a_dispatch_2'),
        edge('a_protect1', 'a_capture1', 'entry'), edge('a_capture1', 'a_pause001'),
        edge('a_pause001', 'a_needfix1'),
        edge('a_predicate_000010', 'a_needfix1', 'result', channel='condition', target_port='b_a_needfix1_0'),
        edge('a_needfix1', 'a_repair01', 'b_a_needfix1_0'),
        edge('a_needfix1', 'a_skipfix1', 'b_a_needfix1_1'),
        edge('a_repair01', 'a_relink1', 'entry'), edge('a_relink1', 'a_recheck1'),
        edge('a_recheck1', 'a_quality1'),
        edge('a_predicate_000012', 'a_predicate_000011', 'result', channel='condition'),
        edge('a_predicate_000014', 'a_predicate_000013', 'result', channel='condition'),
        edge('a_predicate_000015', 'a_predicate_000013', 'result', channel='condition'),
        edge('a_predicate_000013', 'a_predicate_000011', 'result', channel='condition'),
        edge('a_predicate_000011', 'a_quality1', 'result', channel='condition', target_port='b_a_quality1_0'),
        edge('a_quality1', 'a_good001', 'b_a_quality1_0'),
        edge('a_quality1', 'a_bad0001', 'b_a_quality1_1'),
        edge('a_protect1', 'a_recover1'),
        edge('a_watch_exit001', 'a_recover1', 'event', channel='condition', target_port='b_a_recover1_0'),
        edge('a_watch_net001', 'a_predicate_000016', 'event', channel='condition'),
        edge('a_watch_exit001', 'a_predicate_000017', 'event', channel='condition'),
        edge('a_predicate_000017', 'a_predicate_000016', 'result', channel='condition'),
        edge('a_predicate_000016', 'a_recover1', 'result', channel='condition', target_port='b_a_recover1_1'),
        edge('a_recover1', 'a_recovergroup', 'b_a_recover1_0'),
        edge('a_recover1', 'a_hold001', 'b_a_recover1_1'),
        edge('a_recover1', 'a_unknown1', 'b_a_recover1_2'),
        edge('a_recovergroup', 'a_recoverspeed', 'entry'),
        edge('a_recoverspeed', 'a_recoversafe'),
        edge('a_predicate_000019', 'a_predicate_000018', 'result', channel='condition'),
        edge('a_predicate_000020', 'a_predicate_000018', 'result', channel='condition'),
        edge('a_predicate_000018', 'a_recoversafe', 'result', channel='condition', target_port='b_a_recoversafe_0'),
        edge('a_recoversafe', 'a_recoverset1', 'b_a_recoversafe_0'),
        edge('a_recoverset1', 'a_recovernote1'),
        edge('a_recoversafe', 'a_recoverset0', 'b_a_recoversafe_1'),
        edge('a_recoverset0', 'a_recovernote0'),
        edge('a_direct1group', 'a_direct1speed', 'entry'),
        edge('a_direct1speed', 'a_direct1safe'),
        edge('a_predicate_000022', 'a_predicate_000021', 'result', channel='condition'),
        edge('a_predicate_000023', 'a_predicate_000021', 'result', channel='condition'),
        edge('a_predicate_000021', 'a_direct1safe', 'result', channel='condition', target_port='b_a_direct1safe_0'),
        edge('a_direct1safe', 'a_direct1set1', 'b_a_direct1safe_0'),
        edge('a_direct1set1', 'a_direct1note1'),
        edge('a_direct1safe', 'a_direct1set0', 'b_a_direct1safe_1'),
        edge('a_direct1set0', 'a_direct1note0'),
    ]
    graph = rule(nodes, edges)
    graph['variables'] = [{'id': 'v_limit01', 'name': '可接受延迟', 'value_type': 'number', 'initial': 80000},
                          {'id': 'v_status1', 'name': '下载状态', 'value_type': 'text', 'initial': 'running'}]
    return graph


@pytest.mark.parametrize('scenario, initial, repair, before_events, late_event, expected, status', [
    ('lag', (True, 180000), (True, 35000), [('steam', 1)], ('steam', 0), ['capture', 'skip_repair', 'resume'], 'running'),
    ('offline', (False, 240000), (True, 35000), [('steam', 1)], ('network', 1), ['capture', 'reconnect', 'repair_ok', 'hold_paused'], 'paused'),
    ('repair_failed', (False, 240000), (False, 260000), [('steam', 1)], ('steam', 0), ['capture', 'reconnect', 'repair_failed', 'keep_paused'], 'paused'),
    ('resume', (True, 30000), (True, 30000), [('steam', 0)], None, ['resume'], 'running'),
    ('idle', (True, 30000), (True, 30000), [], None, ['idle'], 'running'),
    ('priority', (True, 180000), (True, 35000), [('steam', 1), ('steam', 0)], None, ['capture', 'skip_repair', 'resume'], 'running'),
])
def test_download_workflow_runs_six_scenarios_through_production_executor(scenario, initial, repair, before_events, late_event, expected, status):
    source, context, observer, executor, records, events = setup(_download_graph())
    executor._status_lookup = lambda identity: {'running': 1, 'queued': 0, 'last_status': 'succeeded'}
    executor._actions_funcs()['speed'] = lambda meta, params, context: {'connected': (initial if params['phase'] == 'first' else repair)[0], 'latency': (initial if params['phase'] == 'first' else repair)[1]}
    for kind, value in before_events:
        observer.record(kind, {'value': value})
    outcome = executor.execute_workflow('one', source, source['name'], context)
    if late_event is not None:
        assert isinstance(outcome, RunSuspension), scenario
        observer.record(late_event[0], {'value': late_event[1]})
        assert outcome.wait.ready
        outcome = executor.execute_workflow('one', source, source['name'], context)
    assert outcome.status == 'succeeded', (scenario, outcome)
    assert records == expected
    assert context['variables']['v_status1'] == status
    if scenario == 'priority':
        assert context['steps']['a_dispatch']['result']['branch'] == graph_index(source['workflow'])['a_dispatch']['branches'][0]['id']


@pytest.mark.parametrize('field, value', [('entry', []), ('entry', {}), ('source', {}), ('target', []), ('port', {})])
def test_malformed_control_identifiers_return_validation_errors(field, value):
    source = rule([trigger('a_watch_start01'), action('a_first01', 'first'), action('a_second1', 'second')],
                  [edge('a_watch_start01', 'a_first01', 'event'), edge('a_first01', 'a_second1')])
    if field == 'entry':
        source['workflow'][field] = value
    else:
        next(item for item in source['workflow']['edges'] if item['source'] == 'a_first01')[field] = value
    assert validate_runtime_structure(source)
    with pytest.raises(ValueError):
        normalize_rule_input([source])


@pytest.mark.parametrize('field, value', [('source', {}), ('target', []), ('port', {})])
def test_malformed_condition_identifiers_raise_structured_validation_error(field, value):
    choice = group('a_choice01', [branch('b_a_choice01_0'), branch('b_a_choice01_1', otherwise=True)])
    source = rule([choice, {'type': 'condition', 'binding_id': 'a_predicate_000001',
                            'parent': 'a_choice01', 'predicate': {'op': 'is_true', 'left': True}}],
        [edge('a_predicate_000001', 'a_choice01', 'result', channel='condition', target_port='b_a_choice01_0')])
    source['workflow']['edges'][0][field] = value
    assert validate_canvas_graph(source['workflow'])
    with pytest.raises(ValueError):
        normalize_rule_input([source])


def test_export_type_inference_understands_literal_template_and_conversion():
    from notmyfault.core.binding_schema import rule_binding_types
    exports = {'literal': {'$literal': 'text'}, 'template': {'$template': ['prefix', 'suffix']},
               'converted': {'$convert': {'value': '3', 'to': 'number'}},
               'nested': {'values': [{'$literal': True}, {'$literal': False}]}}
    outer = {'type': 'action_group', 'binding_id': 'a_outer01', 'exports': exports}
    source = rule([trigger('a_watch_start01'), outer], [edge('a_watch_start01', 'a_outer01', 'event')])
    schema = rule_binding_types(source, TRIGGERS, ACTIONS)['step']['a_outer01']['properties']


def test_late_sensitive_event_remains_masked_through_assignment_and_group_export():
    from notmyfault.tests.api_support import create_test_engine, event_fields
    from notmyfault.values import contains_secret
    waited = group('a_wait001', [branch('b_wait_wait001'), branch('b_a_wait001_0', otherwise=True)],
                   mode='wait', parent='a_outer01')
    assignment = {'type': 'set_variable', 'binding_id': 'a_assign1', 'parent': 'a_outer01',
                  'variable': 'v_secret1',
                  'value': {'$ref': {'scope': 'trigger', 'node': 't_steam01', 'path': ['token']}}}
    outer = {'type': 'action_group', 'binding_id': 'a_outer01',
             'exports': {'token': {'$ref': {'scope': 'variable', 'node': 'v_secret1', 'path': []}}}}
    enter = {'type': 'merge', 'binding_id': 'a_enter01', 'parent': 'a_outer01'}
    steam = trigger('a_watch_steam01', 'steam', parent='a_wait001')
    output = {'type': 'record', 'binding_id': 'a_record1', 'params': {'label': {'$ref': {'scope': 'step', 'node': 'a_outer01', 'path': ['token']}}}}
    source = rule([trigger('a_watch_start01'), outer, enter, waited, assignment, steam, output], [
        edge('a_watch_start01', 'a_outer01', 'event'), edge('a_outer01', 'a_enter01', 'entry'),
        edge('a_enter01', 'a_wait001'), edge('a_watch_steam01', 'a_wait001', 'event', channel='condition', target_port='b_wait_wait001'),
        edge('a_wait001', 'a_assign1', 'b_wait_wait001'), edge('a_wait001', 'a_assign1', 'b_a_wait001_0'),
        edge('a_outer01', 'a_record1')])
    source['variables'] = [{'id': 'v_secret1', 'name': '令牌', 'value_type': 'text'}]
    packets, calls, contexts = [], [], []
    engine = create_test_engine({'rules': [source]}, on_event=lambda event: packets.append(event.to_dict()))
    engine.triggers_meta.update(TRIGGERS)
    engine.triggers_meta['steam'] = {'params': [], 'outputs': [{'name': 'token', 'type': 'text', 'sensitive': True}]}
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params, context: calls.append(str(action_values(params['label'])))
    actual_execute = engine._run_manager._execute
    def execute(key, source, name, context):
        contexts.append(context)
        return actual_execute(key, source, name, context)
    engine._run_manager._execute = execute
    try:
        engine.emit_event('start', {'value': 1})
        eventually(lambda: any(context.get('_graph_execution') is not None for context in contexts))
        engine.emit_event('steam', {'token': 'late-secret'})
        assert engine._run_manager.wait_for_idle(3)
        assert engine.events.flush()
        assert calls == ['late-secret']
        context = contexts[0]
        assert contains_secret(context['triggers']['t_steam01']['payload']['token'])
        assert contains_secret(context['steps']['a_outer01']['result']['token'])
        succeeded = [packet for packet in packets if packet['type'] == 'run.step.succeeded']
        assert succeeded and 'late-secret' not in str(packets)
        assert next(packet for packet in succeeded if packet['step_id'] == 'a_outer01')['data']['result']['token'] == '***'
    finally:
        engine.shutdown(3)


def test_group_export_type_uses_dependencies_instead_of_node_array_order():
    from notmyfault.core.binding_schema import rule_binding_types
    producer = {'type': 'action_group', 'binding_id': 'a_first01', 'exports': {'value': {'$literal': 12}}}
    consumer = {'type': 'action_group', 'binding_id': 'a_second1',
                'exports': {'value': {'$ref': {'scope': 'step', 'node': 'a_first01', 'path': ['value']}}}}
    source = rule([trigger('a_watch_start01'), consumer, producer], [
        edge('a_watch_start01', 'a_first01', 'event'), edge('a_first01', 'a_second1')])
    schemas = rule_binding_types(source, TRIGGERS, ACTIONS)['step']
    assert schemas['a_second1']['properties']['value']['type'] == schemas['a_first01']['properties']['value']['type']
    assert schemas['a_second1']['properties']['value']['type'] in ('int', 'number')
    assert validate_runtime_bindings(source, TRIGGERS, ACTIONS) == []


def test_group_export_failure_records_failed_step_and_closed_generator():
    outer = {'type': 'action_group', 'binding_id': 'a_outer01',
             'exports': {'value': {'$ref': {'scope': 'variable', 'node': 'v_uninit1', 'path': [], 'on_missing': 'error'}}}}
    source = rule([trigger('a_watch_start01'), outer], [edge('a_watch_start01', 'a_outer01', 'event')])
    source['variables'] = [{'id': 'v_uninit1', 'name': '未初始化', 'value_type': 'text'}]
    source, context, observer, executor, records, events = setup(source)
    assert executor.execute_workflow('one', source, source['name'], context).status == 'failed'
    assert context['steps']['a_outer01']['status'] == 'failed'
    assert any(kind == 'run.step.failed' and data['step_id'] == 'a_outer01' for kind, data in events)
    assert '_graph_execution' not in context


def test_nested_cancellation_records_all_open_groups_and_clears_execution_position():
    source = waiting_rule()
    graph = source['workflow']
    graph['nodes'].append({'type': 'action_group', 'binding_id': 'a_outer01'})
    for identity in ('a_before1', 'a_wait001', 'a_after01'):
        next(node for node in graph['nodes'] if node['binding_id'] == identity)['parent'] = 'a_outer01'
    graph['edges'] = [connection for connection in graph['edges']
                      if not (connection['source'] == 'a_watch_start01' and connection['target'] == 'a_before1')]
    graph['edges'].extend([edge('a_watch_start01', 'a_outer01', 'event'),
                           edge('a_outer01', 'a_before1', 'entry')])
    source, context, observer, executor, records, events = setup(source)
    manager = RunManager(executor.execute_workflow, max_workers=1)
    try:
        manager.submit('one', source, source['name'], context)
        eventually(lambda: any(kind == 'run.step.waiting' for kind, data in events))
        assert manager.cancel_run('run_graph')
        assert manager.wait_for_idle(3)
        cancelled = {data['step_id'] for kind, data in events if kind == 'run.step.cancelled'}
        assert cancelled == {'a_wait001', 'a_outer01'}
        assert '_graph_execution' not in context
        assert context.get('_group_path') == []
        assert observer.next_deadline() is None
    finally:
        manager.shutdown()


def test_partial_nested_node_keeps_preceding_parent_data_and_group_path():
    from notmyfault.core.workflow_graph import find_graph_scope
    producer = {'type': 'speed', 'binding_id': 'a_speed01'}
    assignment = {'type': 'set_variable', 'binding_id': 'a_assign1', 'parent': 'a_inner01',
                  'variable': 'v_result1', 'value': {'$ref': {'scope': 'step', 'node': 'a_speed01', 'path': ['latency']}}}
    inner = {'type': 'action_group', 'binding_id': 'a_inner01'}
    source = rule([trigger('a_watch_start01'), producer, inner, assignment], [
        edge('a_watch_start01', 'a_speed01', 'event'), edge('a_speed01', 'a_inner01'),
        edge('a_inner01', 'a_assign1', 'entry')])
    source['variables'] = [{'id': 'v_result1', 'name': '延迟', 'value_type': 'number', 'initial': 0}]
    source, context, observer, executor, records, events = setup(source)
    scope = find_graph_scope(source['workflow'], 'a_assign1')
    assert scope['available'] == {'a_speed01', 'a_watch_start01'}
    assert scope['group_path'] == ('a_inner01',)
    context['manual_test'] = {'start_step_id': 'a_assign1', 'end_step_id': 'a_assign1'}
    context['steps']['a_speed01'] = {'type': 'speed', 'status': 'ok', 'result': {'latency': 123}, 'provided': True}
    assert executor.execute_workflow('one', source, source['name'], context).status == 'succeeded'
    assert context['variables']['v_result1'] == 123
    assert next(data for kind, data in events if kind == 'run.step.started')['group_path'] == ['a_inner01']


@pytest.mark.parametrize('kind', ['action_group', 'set_variable'])
@pytest.mark.parametrize('supplied', ['provided-secret', 123])
def test_partial_nested_run_accepts_conditional_native_output_with_type_and_secret_protection(kind, supplied):
    from notmyfault.core.workflow_graph import find_graph_scope
    from notmyfault.tests.api_support import create_test_engine
    from notmyfault.values import contains_secret
    choose = group('a_choice1', [branch('b_a_choice1_0'), branch('b_a_choice1_1', otherwise=True)])
    if kind == 'action_group':
        producer = {'type': kind, 'binding_id': 'a_source1',
                    'exports': {'value': {'$ref': {'scope': 'variable', 'node': 'v_secret1', 'path': []}}}}
    else:
        producer = {'type': kind, 'binding_id': 'a_source1', 'variable': 'v_secret1', 'value': 'initial-secret'}
    target = action('a_target1', {'$ref': {'scope': 'step', 'node': 'a_source1', 'path': ['value'], 'on_missing': 'default', 'default': 'missing'}}) | {'parent': 'a_inner01'}
    inner = {'type': 'action_group', 'binding_id': 'a_inner01'}
    condition = {'type': 'condition', 'binding_id': 'a_predicate_000001', 'parent': 'a_choice1',
                 'predicate': {'op': 'is_true', 'left': True}}
    source = rule([trigger('a_watch_start01'), choose, condition, producer, inner, target], [
        edge('a_watch_start01', 'a_choice1', 'event'),
        edge('a_predicate_000001', 'a_choice1', 'result', channel='condition', target_port='b_a_choice1_0'),
        edge('a_choice1', 'a_source1', 'b_a_choice1_0'), edge('a_source1', 'a_inner01'),
        edge('a_choice1', 'a_inner01', 'b_a_choice1_1'), edge('a_inner01', 'a_target1', 'entry')])
    source['variables'] = [{'id': 'v_secret1', 'name': '令牌', 'value_type': 'text', 'sensitive': True, 'initial': 'initial-secret'}]
    scope = find_graph_scope(source['workflow'], 'a_target1')
    assert scope['available'] == {'a_choice1', 'a_watch_start01'}
    assert scope['possible'] == {'a_choice1', 'a_source1', 'a_watch_start01'}
    assert validate_runtime_bindings(source, TRIGGERS, ACTIONS) == []
    calls, packets, contexts = [], [], []
    engine = create_test_engine({'rules': []}, on_event=lambda event: packets.append(event.to_dict()))
    engine.triggers_meta.update(TRIGGERS)
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params, context: calls.append(str(action_values(params['label'])))
    execute = engine._run_manager._execute
    def capture(key, rule, name, context):
        contexts.append(context)
        return execute(key, rule, name, context)
    engine._run_manager._execute = capture
    try:
        ok, message, _run_id = engine.run_manual_rule_snapshot(source, start_step_id='a_target1', end_step_id='a_target1',
            step_outputs={'a_source1': {'value': supplied}, 'a_target1': {'value': 'future'}})
        assert ok is isinstance(supplied, str)
        if ok:
            assert engine._run_manager.wait_for_idle(3)
            assert engine.events.flush()
            assert calls == ['provided-secret']
            assert contains_secret(contexts[0]['steps']['a_source1']['result']['value'])
            assert 'provided-secret' not in str(packets)
            assert contexts[0]['steps']['a_target1'].get('provided') is not True
        else:
            assert calls == [] and '文本' in message
    finally:
        engine.shutdown(3)


@pytest.mark.parametrize('termination', ['null', 'end'])
@pytest.mark.parametrize('policy', [None, 'skip', 'default'])
def test_group_export_checks_every_terminal_branch_and_handles_missing_policy(termination, policy):
    choose = group('a_choice1', [branch('b_a_choice1_0'), branch('b_a_choice1_1', otherwise=True)],
                   parent='a_outer01')
    speed = {'type': 'speed', 'binding_id': 'a_speed01', 'parent': 'a_outer01', 'params': {}}
    nodes = [trigger('a_watch_start01'), {'type': 'action_group', 'binding_id': 'a_outer01'}, choose, speed,
             {'type': 'condition', 'binding_id': 'a_predicate_000001', 'parent': 'a_choice1',
              'predicate': {'op': 'is_true', 'left': False}}]
    edges = [edge('a_watch_start01', 'a_outer01', 'event'),
             edge('a_outer01', 'a_choice1', 'entry'),
             edge('a_predicate_000001', 'a_choice1', 'result', channel='condition', target_port='b_a_choice1_0'),
             edge('a_choice1', 'a_speed01', 'b_a_choice1_0')]
    if termination == 'end':
        nodes.append({'type': 'end', 'binding_id': 'a_end0001', 'parent': 'a_outer01'})
        edges.append(edge('a_choice1', 'a_end0001', 'b_a_choice1_1'))
    reference = {'scope': 'step', 'node': 'a_speed01', 'path': ['latency']}
    if policy:
        reference['on_missing'] = policy
    if policy == 'default':
        reference['default'] = 123
    outer = nodes[1]
    outer['exports'] = {'latency': {'$ref': reference}}
    nodes.append(action('a_after01', 'after'))
    edges.append(edge('a_outer01', 'a_after01'))
    source = rule(nodes, edges)
    errors = validate_runtime_bindings(source, TRIGGERS, ACTIONS)
    if policy is None:
        assert errors and any(error['code'] == 'conditional_source' for error in errors)
        return
    assert errors == []
    source, context, observer, executor, records, events = setup(source)
    assert executor.execute_workflow('one', source, source['name'], context).status == 'succeeded'
    assert records == ['after']
    result = context['steps']['a_outer01']
    if policy == 'skip':
        assert result['status'] == 'skipped' and result['result'] is None
        assert any(kind == 'run.step.skipped' and data['step_id'] == 'a_outer01' for kind, data in events)
        assert not context['_open_groups']
    else:
        assert result['status'] == 'ok' and result['result']['latency'] == 123


@pytest.mark.parametrize('kind', ['event_group', 'action_group'])
@pytest.mark.parametrize('field', ['params', 'retry', 'retry_delay_seconds', 'retry_backoff', 'failure_actions', 'timeout_seconds'])
def test_native_groups_reject_ignored_action_settings(kind, field):
    if kind == 'event_group':
        node = group('a_group01', [branch('b_a_group01_0'), branch('b_a_group01_1', otherwise=True)])
        nodes = [trigger('a_watch_start01'), node,
                 {'type': 'condition', 'binding_id': 'a_predicate_000001', 'parent': 'a_group01',
                  'predicate': {'op': 'is_true', 'left': True}}]
        edges = [edge('a_watch_start01', 'a_group01', 'event'),
                 edge('a_predicate_000001', 'a_group01', 'result', channel='condition', target_port='b_a_group01_0')]
    else:
        node = {'type': kind, 'binding_id': 'a_group01'}
        nodes = [trigger('a_watch_start01'), node]
        edges = [edge('a_watch_start01', 'a_group01', 'event')]
    node[field] = {'params': {}, 'retry': 1, 'retry_delay_seconds': 1, 'retry_backoff': 2,
                   'failure_actions': [], 'timeout_seconds': 1}[field]
    source = rule(nodes, edges)
    errors = validate_runtime_structure(source)
    assert any((field if field != 'failure_actions' else '已移除') in error for error in errors)


def test_event_group_rejects_error_continuation_and_timeout_stays_failed():
    source = waiting_rule(timeout=1)
    waited = next(node for node in source['workflow']['nodes'] if node['type'] == 'event_group')
    waited['on_error'] = 'continue'
    assert any('on_error' in error for error in validate_runtime_structure(source))
    waited.pop('on_error')
    assert validate_runtime_structure(source) == []
    clock = FakeClock()
    source, context, observer, executor, records, events = setup(source, clock)
    suspension = executor.execute_workflow('one', source, source['name'], context)
    assert isinstance(suspension, RunSuspension)
    clock.advance(1)
    observer.process_timers()
    assert executor.execute_workflow('one', source, source['name'], context).status == 'timed_out'
    assert records == ['before']
    assert not any(kind == 'run.group.selected' and data['step_id'] == 'a_wait001' for kind, data in events)


@pytest.mark.parametrize('change', ['modify', 'delete'])
def test_reload_finishes_waiting_run_without_more_events_and_releases_old_generation(change):
    from notmyfault.tests.api_support import create_test_engine
    packets, calls, contexts = [], [], []
    source = waiting_rule()
    engine = create_test_engine({'rules': []}, on_event=lambda event: packets.append(event.to_dict()))
    engine.triggers_meta.update(TRIGGERS)
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params, context: calls.append(params['label'])
    execute = engine._run_manager._execute
    def capture(key, rule, name, context):
        contexts.append(context)
        return execute(key, rule, name, context)
    engine._run_manager._execute = capture
    released = threading.Event()
    try:
        old = engine._generations.apply([source], 'test', activate=False, validate=False)
        old.references.on_release = released.set
        engine.emit_event('start', {'value': 1})
        eventually(lambda: contexts and contexts[0].get('_graph_execution') is not None)
        eventually(lambda: any(run.suspension is not None for run in engine._run_manager._runs.values()))
        assert old.references.count == 2
        changed = copy.deepcopy(source)
        next(node for node in changed['workflow']['nodes'] if node.get('binding_id') == 'a_after01')['params']['label'] = '修改后的动作'
        engine._generations.apply([changed] if change == 'modify' else [], 'test', activate=False, validate=False)
        assert engine._run_manager.wait_for_idle(3)
        assert released.wait(3) and old.references.count == 0
        assert engine.events.flush()
        assert calls == ['before']
        terminals = [packet for packet in packets if packet['type'] in ('run.cancelled', 'run.succeeded', 'run.failed', 'run.timed_out')]
        assert len(terminals) == 1 and terminals[0]['type'] == 'run.cancelled'
        assert not contexts[0].get('_open_groups')
        assert '_graph_execution' not in contexts[0]
        assert contexts[0]['_event_observation'].next_deadline() is None
    finally:
        engine.shutdown(3)


@pytest.mark.parametrize('first', [True, False])
def test_group_records_only_evaluated_branches_and_short_circuited_predicates(first, tmp_path):
    from notmyfault.core.run_history import RunHistory

    choose = group('a_choice1', [branch('b_a_choice1_0'), branch('b_a_choice1_1'),
                                 branch('b_a_choice1_2', otherwise=True)])
    nodes = [trigger('a_watch_start01'), choose,
        {'type': 'logic', 'binding_id': 'a_predicate_000001', 'parent': 'a_choice1', 'op': 'all'},
        {'type': 'condition', 'binding_id': 'a_predicate_000002', 'parent': 'a_choice1',
         'predicate': {'op': 'is_true', 'left': first}},
        {'type': 'condition', 'binding_id': 'a_predicate_000003', 'parent': 'a_choice1',
         'predicate': {'op': 'is_true', 'left': True}},
        {'type': 'condition', 'binding_id': 'a_predicate_000004', 'parent': 'a_choice1',
         'predicate': {'op': 'is_true', 'left': True}}, action('a_after01', 'after')]
    edges = [edge('a_watch_start01', 'a_choice1', 'event'),
        edge('a_predicate_000002', 'a_predicate_000001', 'result', channel='condition'),
        edge('a_predicate_000003', 'a_predicate_000001', 'result', channel='condition'),
        edge('a_predicate_000001', 'a_choice1', 'result', channel='condition', target_port=choose['branches'][0]['id']),
        edge('a_predicate_000004', 'a_choice1', 'result', channel='condition', target_port=choose['branches'][1]['id']),
        *(edge('a_choice1', 'a_after01', branch['id']) for branch in choose['branches'][:2])]
    source = rule(nodes, edges)
    source, context, observer, executor, records, events = setup(source)
    assert executor.execute_workflow('one', source, source['name'], context).status == 'succeeded'
    selected = next(data for kind, data in events if kind == 'run.group.selected' and data['step_id'] == 'a_choice1')
    evaluated = selected['evaluated_branches']
    assert [(item['branch_id'], item['result']) for item in evaluated] == (
        [(choose['branches'][0]['id'], True)] if first else [(choose['branches'][0]['id'], False), (choose['branches'][1]['id'], True)])
    assert len(evaluated[0]['predicates']) == (3 if first else 2)
    assert [item['op'] for item in evaluated[0]['predicates']] == (
        ['is_true', 'is_true', 'all'] if first else ['is_true', 'all'])
    assert all(item.get('node_id') for record in evaluated for item in record['predicates'])
    history = RunHistory(str(tmp_path / 'runs.jsonl'))
    history.record_async({'v': 1, 'type': 'run.triggered', 'data': {'run_id': 'run_graph'}})
    for kind, data in events:
        history.record_async({'v': 1, 'type': kind, 'data': data})
    assert history.flush()
    saved = next(step for step in RunHistory(history.path).get_run('run_graph')['steps'] if step['step_id'] == 'a_choice1')
    assert saved['evaluated_branches'] == evaluated
    assert saved['branch'] == choose['branches'][0 if first else 1]['id']


def test_group_condition_error_is_recorded_without_false_or_else_selection(tmp_path):
    from notmyfault.core.run_history import RunHistory
    choose = group('a_choice1', [branch('b_a_choice1_0'), branch('b_a_choice1_1'),
                                 branch('b_a_choice1_2', otherwise=True)])
    nodes = [trigger('a_watch_start01'), choose,
        {'type': 'condition', 'binding_id': 'a_predicate_000001', 'parent': 'a_choice1',
         'predicate': {'op': 'is_true', 'left': False}},
        {'type': 'condition', 'binding_id': 'a_predicate_000002', 'parent': 'a_choice1',
         'predicate': {'op': 'eq', 'left': {'$ref': {'scope': 'trigger', 'node': 't_steam01', 'path': ['value']}}, 'right': 1}},
        trigger('a_watch_steam01', 'steam')]
    source = rule(nodes, [
        edge('a_watch_start01', 'a_choice1', 'event'),
        edge('a_predicate_000001', 'a_choice1', 'result', channel='condition', target_port='b_a_choice1_0'),
        edge('a_predicate_000002', 'a_choice1', 'result', channel='condition', target_port='b_a_choice1_1')])
    source, context, observer, executor, records, events = setup(source)
    assert executor.execute_workflow('one', source, source['name'], context).status == 'failed'
    assert not any(kind == 'run.group.selected' and data['step_id'] == 'a_choice1' for kind, data in events)
    failed = next(data for kind, data in events if kind == 'run.step.failed' and data['step_id'] == 'a_choice1')
    evaluated = failed['evaluated_branches']
    assert evaluated[0]['result'] is False
    assert 'result' not in evaluated[1] and evaluated[1]['error']['code'] == 'missing_binding_value'
    assert 'result' not in evaluated[1]['predicates'][0]
    history = RunHistory(str(tmp_path / 'runs.jsonl'))
    history.record_async({'v': 1, 'type': 'run.triggered', 'data': {'run_id': 'run_graph'}})
    for kind, data in events:
        history.record_async({'v': 1, 'type': kind, 'data': data})
    assert history.flush()
    restored = next(step for step in RunHistory(history.path).get_run('run_graph')['steps'] if step['step_id'] == 'a_choice1')
    assert restored['evaluated_branches'] == evaluated and 'branch' not in restored


def test_wait_with_plain_not_resumes_on_positive_event_without_absence_timer():
    source = {
        'rule_id': 'r_graph001', 'name': '网络恢复等待',
        'workflow': {'entry': None, 'nodes': [
            {'binding_id': 'a_start001', 'type': 'trigger', 'trigger': leaf()},
            {'binding_id': 'a_network1', 'type': 'trigger', 'trigger': leaf('network', 't_net001')},
            {'binding_id': 'a_steam001', 'type': 'trigger', 'trigger': leaf('steam', 't_steam01')},
            {'binding_id': 'a_not00001', 'type': 'logic', 'op': 'not'},
            {'binding_id': 'a_and00001', 'type': 'logic', 'op': 'all'},
            {'binding_id': 'a_wait0001', 'type': 'event_group', 'group_type': 'if', 'mode': 'wait',
             'branches': [{'id': 'b_resume01'}, {'id': 'b_else0001', 'else': True}]},
            action('a_after001', 'after'),
        ], 'edges': [
            {'source': 'a_start001', 'port': 'event', 'target': 'a_wait0001', 'channel': 'control'},
            {'source': 'a_steam001', 'port': 'event', 'target': 'a_not00001', 'channel': 'condition'},
            {'source': 'a_network1', 'port': 'event', 'target': 'a_and00001', 'channel': 'condition'},
            {'source': 'a_not00001', 'port': 'result', 'target': 'a_and00001', 'channel': 'condition'},
            {'source': 'a_and00001', 'port': 'result', 'target': 'a_wait0001', 'target_port': 'b_resume01', 'channel': 'condition'},
            {'source': 'a_wait0001', 'port': 'b_resume01', 'target': 'a_after001', 'channel': 'control'},
        ]},
    }
    source, context, observer, executor, records, _events = setup(source)
    suspended = executor.execute_workflow('one', source, source['name'], context)
    assert isinstance(suspended, RunSuspension)
    assert observer.next_deadline() is None
    observer.record('network', {'value': 1})
    assert suspended.wait.ready
    assert executor.execute_workflow('one', source, source['name'], context).status == 'succeeded'
    assert records == ['after']


@pytest.mark.parametrize('positive_input', [False, True])
def test_timed_not_entry_counts_a_full_window_with_or_without_and_input(positive_input):
    clock = FakeClock()
    nodes = [
        {'binding_id': 'a_steam001', 'type': 'trigger', 'trigger': leaf('steam', 't_steam01')},
        {'binding_id': 'a_not00001', 'type': 'logic', 'op': 'not', 'duration_seconds': 2},
        action('a_after001', 'after'),
    ]
    edges = [{'source': 'a_steam001', 'port': 'event', 'target': 'a_not00001', 'channel': 'condition'}]
    root = 'a_not00001'
    if positive_input:
        nodes.extend([
            {'binding_id': 'a_network1', 'type': 'trigger', 'trigger': leaf('network', 't_net001')},
            {'binding_id': 'a_and00001', 'type': 'logic', 'op': 'all'},
        ])
        edges.extend([
            {'source': 'a_network1', 'port': 'event', 'target': 'a_and00001', 'channel': 'condition'},
            {'source': 'a_not00001', 'port': 'result', 'target': 'a_and00001', 'channel': 'condition'},
        ])
        root = 'a_and00001'
    edges.append({'source': root, 'port': 'result', 'target': 'a_after001', 'channel': 'control'})
    source = {'rule_id': 'r_graph001', 'name': '连续未发生', 'workflow': {'entry': None, 'nodes': nodes, 'edges': edges}}
    prepared = prepare_rules([source], TRIGGERS, ACTIONS, validate=False, clock=clock)
    runtime = prepared.runtime
    runtime.register_rule(source['rule_id'], source, 0)
    if positive_input:
        assert not runtime.match_and_take(source['rule_id'], source, 'network', {'value': 1}, now=0)
    assert runtime.take_due(1.999) == []
    matches = runtime.take_due(2)
    assert len(matches) == 1 and matches[0][0] == source['rule_id']
    assert runtime.take_due(4) == []
    assert not runtime.match_and_take(source['rule_id'], source, 'steam', {'value': 1}, now=4)
    if positive_input:
        assert not runtime.match_and_take(source['rule_id'], source, 'network', {'value': 2}, now=4)
    assert runtime.take_due(5.999) == []
    assert len(runtime.take_due(6)) == 1
