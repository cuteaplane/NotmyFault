import copy
import threading
import time

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
from notmyfault.core.workflow_graph import (
    condition_graph_predicate, predicate_condition_graph, normalize_workflow_rule,
    get_all_rule_events, graph_index, validate_workflow_graph,
)


TRIGGERS = {name: {'params': [], 'outputs': [{'name': 'value', 'type': 'number'}]} for name in ('start', 'steam', 'network')}
ACTIONS = {
    'record': {'params': [{'name': 'label', 'type': 'text'}], 'outputs': []},
    'speed': {'params': [], 'outputs': [{'name': 'latency', 'type': 'number'}, {'name': 'connected', 'type': 'bool'}]},
}


def leaf(kind='start', identity='t_start01'):
    return {'type': kind, 'binding_id': identity, 'params': {}}


def action(identity, label):
    return {'type': 'record', 'binding_id': identity, 'params': {'label': label}}


def edge(source, target, port='next'):
    return {'source': source, 'target': target, 'port': port}


def branch(identity, predicate=None):
    return {'id': identity, 'condition_graph': predicate_condition_graph(predicate)} if predicate is not None else {'id': identity, 'else': True}


def group(identity, predicates, **fields):
    return {'type': 'event_group', 'binding_id': identity, 'mode': 'immediate',
            'branches': [branch(f'b_{identity}_{i}', predicate) for i, predicate in enumerate(predicates)], **fields}


def rule(nodes, edges, entry=None):
    return {'rule_id': 'r_graph001', 'name': '图执行', '_matcher': leaf(),
            'workflow': {'entry': entry or nodes[0]['binding_id'], 'nodes': nodes, 'edges': edges}}


def validate_runtime_structure(source):
    return validate_workflow_graph(source['workflow'])


def validate_runtime_bindings(source, triggers, actions):
    return check_rule_bindings(source, triggers, actions)


def setup(rule, clock=None, record=None):
    records, events = [], []
    source = copy.deepcopy(rule)
    registry = TypeRegistry.from_plugins(TRIGGERS, ACTIONS)
    context = build_context(source['name'], 'start', {}, [], source['rule_id'], 'run_graph')
    prepare_binding_context(source, context, TRIGGERS, ACTIONS, registry=registry)
    compiled = compile_rule(source, context, TRIGGERS, ACTIONS,
                            {name: output_type(meta) for name, meta in TRIGGERS.items()}, registry)
    source = compiled.source
    observer = RunEventObservation(compiled, clock=clock)
    context['_compiled_rule'] = compiled
    context['_compiled_refs'] = compiled.references
    context['_event_observation'] = observer
    functions = {'record': record or (lambda meta, params: records.append(params['label'])),
                 'speed': lambda meta, params: {'latency': 180000, 'connected': True}}
    executor = WorkflowExecutor(actions_meta=lambda: ACTIONS, triggers_meta=lambda: TRIGGERS,
        actions_funcs=lambda: functions, plugin_modules=lambda: {}, diagnostics=Diagnostics(),
        shutdown_event=lambda: None, on_event=lambda kind, data: events.append((kind, data)), clock=clock)
    return source, context, observer, executor, records, events



def canvas_fixture(runtime, *, internal_start=False):
    from notmyfault.core.conditions import iter_condition_events
    from notmyfault.core.workflow_graph import branch_predicate
    native = {key: copy.deepcopy(value) for key, value in runtime.items() if key not in ('workflow', '_matcher')}
    graph = {'entry': None, 'nodes': [], 'edges': []}
    native['workflow'] = graph
    triggers, count = {}, 0
    def source(leaf, parent=None):
        identity = leaf['binding_id']
        if identity not in triggers:
            wrapper = {'type': 'trigger', 'binding_id': 'a_watch_' + identity[2:], 'trigger': copy.deepcopy(leaf)}
            if parent:
                wrapper['parent'] = parent
            graph['nodes'].append(wrapper)
            triggers[identity] = wrapper
        return triggers[identity]['binding_id'], 'event'
    def predicate(value, parent):
        nonlocal count
        if value['op'] == 'event_occurred':
            wrapper, port = source(leaf_specs[value['trigger_id']], parent)
            if value.get('expected', True):
                return wrapper, port
            value = {'op': 'not', 'children': [{**value, 'expected': True}]}
        count += 1
        identity = f'a_predicate_{count:06d}'
        logical = value['op'] in ('all', 'any', 'not')
        node = {'binding_id': identity, 'type': 'logic' if logical else 'condition', 'parent': parent}
        node.update({'op': value['op']} if logical else {'predicate': copy.deepcopy(value)})
        graph['nodes'].append(node)
        if logical:
            for child in value['children']:
                child_id, port = predicate(child, parent)
                graph['edges'].append({'source': child_id, 'port': port, 'target': identity, 'channel': 'condition'})
        return identity, 'result'
    leaf_specs = {item['binding_id']: item for item in get_all_rule_events(runtime)}
    if runtime.get('_matcher'):
        leaf_specs.update({item['binding_id']: item for item in iter_condition_events(runtime['_matcher'])})
    def flatten(current, parent=None):
        for item in current['nodes']:
            node = {key: copy.deepcopy(value) for key, value in item.items() if key not in ('workflow', 'activation', 'triggers', 'branches')}
            if parent:
                node['parent'] = parent
            graph['nodes'].append(node)
            if item['type'] == 'action_group':
                entry = item['workflow']['entry']
                flatten(item['workflow'], item['binding_id'])
                if entry:
                    graph['edges'].append({'source': item['binding_id'], 'port': 'entry', 'target': entry, 'channel': 'control'})
            elif item['type'] == 'event_group':
                node.update(group_type='if', branches=[])
                for branch in item['branches']:
                    node['branches'].append({'id': branch['id'], **({'else': True} if branch.get('else') else {})})
                    if not branch.get('else'):
                        condition_id, port = predicate(branch_predicate(branch), item['binding_id'])
                        graph['edges'].append({'source': condition_id, 'port': port, 'target': item['binding_id'], 'target_port': branch['id'], 'channel': 'condition'})
                if item.get('mode') == 'wait' and not any(not branch.get('else') for branch in item['branches']):
                    event_id, port = source(item['activation'], item['binding_id'])
                    branch_id = 'b_wait_' + item['binding_id'][2:]
                    node['branches'].insert(0, {'id': branch_id})
                    graph['edges'].append({'source': event_id, 'port': port, 'target': item['binding_id'], 'target_port': branch_id, 'channel': 'condition'})
                    exits = [edge for edge in current['edges'] if edge['source'] == item['binding_id']]
                    if exits and exits[0]['target']:
                        graph['edges'].append({'source': item['binding_id'], 'port': branch_id, 'target': exits[0]['target'], 'channel': 'control'})
        graph['edges'].extend({**edge, 'channel': 'control'} for edge in current['edges'] if edge['target'] is not None)
    flatten(runtime['workflow'])
    if not internal_start:
        trigger_id, port = source(runtime.get('_matcher') or leaf())
        graph['edges'].append({'source': trigger_id, 'port': port, 'target': runtime['workflow']['entry'], 'channel': 'control'})
    return native

def eventually(test, timeout=3):
    deadline = time.monotonic() + timeout
    while not test() and time.monotonic() < deadline:
        threading.Event().wait(.005)
    assert test()


def test_conditions_follow_edges_and_short_circuit_in_input_order():
    graph = predicate_condition_graph({'op': 'any', 'children': [
        {'op': 'is_true', 'left': True},
        {'op': 'eq', 'left': {'$ref': {'scope': 'step', 'node': 'a_missing', 'path': []}}, 'right': 1},
    ]})
    from notmyfault.core.predicates import evaluate_predicate
    assert evaluate_predicate(condition_graph_predicate(graph), {})
    graph['edges'][0]['order'], graph['edges'][1]['order'] = 1, 0
    with pytest.raises(ValueError):
        evaluate_predicate(condition_graph_predicate(graph), {})
    graph['edges'].append({'source': graph['root'], 'target': graph['root'], 'order': 2})
    with pytest.raises(ValueError, match='环'):
        condition_graph_predicate(graph)


def test_control_connections_override_node_array_order_and_merge_once():
    decision = group('a_decide01', [{'op': 'is_true', 'left': True}, {'op': 'is_true', 'left': True}, None])
    nodes = [action('a_else001', 'else'), action('a_after01', 'after'), decision,
             action('a_elif001', 'elif'), action('a_first01', 'first')]
    graph = rule(nodes, [edge('a_decide01', 'a_first01', decision['branches'][0]['id']),
        edge('a_decide01', 'a_elif001', decision['branches'][1]['id']),
        edge('a_decide01', 'a_else001', decision['branches'][2]['id']),
        edge('a_first01', 'a_after01'), edge('a_elif001', 'a_after01'), edge('a_else001', 'a_after01'), edge('a_after01', None)], 'a_decide01')
    source, context, observer, executor, records, events = setup(graph)
    assert executor.execute_workflow('key', source, source['name'], context).status == 'succeeded'
    assert records == ['first', 'after']
    assert context['steps']['a_decide01']['result']['branch'] == decision['branches'][0]['id']
    assert [event[0] for event in events].count('run.group.selected') == 1


def test_old_rule_conversion_is_rejected_without_changing_binding_ids():
    old = {'rule_id': 'r_graph001', 'name': '转换', 'condition': leaf(), 'actions': [
        {'type': 'if', 'binding_id': 'a_decide01', 'condition': {'op': 'is_true', 'left': False},
         'then': [action('a_first01', 'first')], 'elif': [{'condition': {'op': 'is_true', 'left': True}, 'then': [action('a_second1', 'second')]}], 'else': []},
        action('a_after01', 'after'),
    ]}
    original = copy.deepcopy(old)
    with pytest.raises(ValueError, match='旧条件模型已移除'):
        normalize_workflow_rule(old)
    assert old == original


def test_nested_groups_export_output_and_reject_direct_inner_reference():
    producer = {'type': 'speed', 'binding_id': 'a_speed01', 'params': {}}
    inner = {'type': 'action_group', 'binding_id': 'a_inner01', 'workflow': {'entry': 'a_speed01', 'nodes': [producer], 'edges': [edge('a_speed01', None)]},
             'exports': {'latency': {'$ref': {'scope': 'step', 'node': 'a_speed01', 'path': ['latency']}}}}
    outer = {'type': 'action_group', 'binding_id': 'a_outer01', 'workflow': {'entry': 'a_inner01', 'nodes': [inner], 'edges': [edge('a_inner01', None)]},
             'exports': {'latency': {'$ref': {'scope': 'step', 'node': 'a_inner01', 'path': ['latency']}}}}
    predicate = {'op': 'gt', 'left': {'$ref': {'scope': 'step', 'node': 'a_outer01', 'path': ['latency']}}, 'right': 80000}
    decision = group('a_decide01', [predicate, None])
    graph = rule([outer, decision, action('a_first01', 'pause')], [edge('a_outer01', 'a_decide01'),
        edge('a_decide01', 'a_first01', decision['branches'][0]['id']), edge('a_decide01', None, decision['branches'][1]['id']), edge('a_first01', None)])
    source, context, observer, executor, records, events = setup(graph)
    assert executor.execute_workflow('key', source, source['name'], context).status == 'succeeded'
    assert records == ['pause'] and context['steps']['a_outer01']['result']['latency'] == 180000
    decision['branches'][0]['condition_graph'] = predicate_condition_graph({**predicate, 'left': {'$ref': {'scope': 'step', 'node': 'a_speed01', 'path': ['latency']}}})
    assert any(issue['code'] == 'forward_reference' for issue in validate_runtime_bindings(graph, TRIGGERS, ACTIONS))


def waiting_rule(timeout=None, activation=None):
    wait = group('a_wait001', [{'op': 'event_occurred', 'trigger_id': 't_steam01'}, None], mode='wait',
        activation=activation or leaf('steam', 't_steam01'), triggers=[leaf('network', 't_net001')])
    if timeout is not None:
        wait['timeout_seconds'] = timeout
    return rule([action('a_before1', 'before'), wait, action('a_after01', 'after')], [edge('a_before1', 'a_wait001'),
        edge('a_wait001', 'a_after01', wait['branches'][0]['id']), edge('a_wait001', 'a_after01', wait['branches'][1]['id']), edge('a_after01', None)])


def test_wait_releases_single_worker_and_late_event_resumes_without_repeating_action():
    source, context, observer, executor, records, events = setup(waiting_rule())
    manager = RunManager(executor.execute_workflow, max_workers=1)
    try:
        assert manager.submit('one', source, source['name'], context) == 'started'
        eventually(lambda: any(kind == 'run.group.waiting' for kind, data in events))
        other = rule([action('a_extra01', 'other')], [edge('a_extra01', None)])
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
            other = rule([action('a_extra01', 'new')], [edge('a_extra01', None)])
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
    activation = {'op': 'all', 'within_seconds': 2, 'children': [leaf('steam', 't_steam01'), leaf('network', 't_net001')]}
    source, context, observer, executor, records, events = setup(waiting_rule(activation=activation), clock)
    observer.record('steam', {'value': 1})
    clock.advance(3)
    observer.record('network', {'value': 1})
    first = executor.execute_workflow('one', source, source['name'], context)
    assert isinstance(first, RunSuspension)
    observer.record('steam', {'value': 2})
    assert first.wait.ready
    assert executor.execute_workflow('one', source, source['name'], context).status == 'succeeded'
    source, context, observer, executor, records, events = setup(waiting_rule(activation={'op': 'not', 'within_seconds': 2, 'children': [leaf('steam', 't_steam01')]}), clock)
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
    (lambda graph: graph['edges'].append(edge('a_second1', 'a_inner01')), '同一图'),
    (lambda graph: graph['nodes'].append(action('a_orphan1', 'unused')), '无法从入口'),
])
def test_graph_validation_rejects_cycle_fanout_cross_scope_and_unreachable(mutate, message):
    graph = rule([action('a_first01', 'first'), action('a_second1', 'second')], [edge('a_first01', 'a_second1'), edge('a_second1', None)])
    mutate(graph['workflow'])
    assert any(message in error for error in validate_runtime_structure(graph))


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
    nested = waiting_rule()['workflow']
    outer = {'type': 'action_group', 'binding_id': 'a_outer01', 'workflow': nested}
    graph = rule([outer, action('a_finish1', 'finish')], [edge('a_outer01', 'a_finish1'), edge('a_finish1', None)])
    source, context, observer, executor, records, events = setup(graph)
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
    decision = group('a_decide01', [{'op': 'is_true', 'left': False}, None])
    producer = {'type': 'speed', 'binding_id': 'a_speed01', 'params': {}}
    ref = {'$ref': {'scope': 'step', 'node': 'a_speed01', 'path': ['latency']}}
    assignment = {'type': 'set_variable', 'binding_id': 'a_assign1', 'variable': 'v_result1', 'value': ref}
    graph = rule([decision, producer, assignment], [edge('a_decide01', 'a_speed01', decision['branches'][0]['id']),
        edge('a_decide01', 'a_assign1', decision['branches'][1]['id']), edge('a_speed01', 'a_assign1'), edge('a_assign1', None)])
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
    decision = group('a_entry01', [{'op': 'event_occurred', 'trigger_id': 't_start01'}, None], activation=leaf())
    middle = group('a_decide01', [{'op': 'event_occurred', 'trigger_id': 't_steam01'}, None], triggers=[leaf('steam', 't_steam01')])
    source = rule([decision, middle, action('a_first01', 'first'), action('a_else001', 'else')], [
        edge('a_entry01', 'a_decide01', decision['branches'][0]['id']), edge('a_entry01', 'a_else001', decision['branches'][1]['id']),
        edge('a_decide01', 'a_first01', middle['branches'][0]['id']), edge('a_decide01', 'a_else001', middle['branches'][1]['id']),
        edge('a_first01', None), edge('a_else001', None)], 'a_entry01')
    source = canvas_fixture(source)
    calls = []
    engine = create_test_engine({'rules': [source]})
    engine.triggers_meta.update(TRIGGERS)
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params: calls.append(params['label'])
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
    decision = group('a_entry01', [{'op': 'event_occurred', 'trigger_id': 't_steam01'}, None], activation=leaf(), triggers=[leaf('steam', 't_steam01')])
    middle = group('a_decide01', [{'op': 'event_occurred', 'trigger_id': 't_steam01'}, None])
    source = rule([decision, middle, action('a_first01', 'first'), action('a_else001', 'middle'), action('a_wrong01', 'wrong')], [
        edge('a_entry01', 'a_wrong01', decision['branches'][0]['id']), edge('a_entry01', 'a_decide01', decision['branches'][1]['id']),
        edge('a_decide01', 'a_else001', middle['branches'][0]['id']), edge('a_decide01', 'a_first01', middle['branches'][1]['id']),
        edge('a_else001', None), edge('a_first01', None), edge('a_wrong01', None)], 'a_entry01')
    source = canvas_fixture(source)
    native_entry = next(node for node in source['workflow']['nodes'] if node['binding_id'] == 'a_entry01')
    native_entry['branches'].insert(1, {'id': 'b_start_elif01'})
    source['workflow']['edges'] = [edge for edge in source['workflow']['edges'] if not (edge['source'] == 'a_watch_start01' and edge['channel'] == 'control')]
    source['workflow']['edges'].append({'source': 'a_watch_start01', 'port': 'event', 'target': 'a_entry01', 'target_port': 'b_start_elif01', 'channel': 'condition'})
    next(edge for edge in source['workflow']['edges'] if edge['source'] == 'a_entry01' and edge['target'] == 'a_decide01')['port'] = 'b_start_elif01'
    calls, packets = [], []
    engine = create_test_engine({'rules': [source]}, on_event=lambda event: packets.append(event.to_dict()))
    engine.triggers_meta.update(TRIGGERS)
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params: calls.append(params['label'])
    manager = RunManager(engine._workflow_executor.execute_workflow, max_workers=1)
    engine._run_manager = manager
    engine._event_bus._status_lookup = manager.rule_status
    engine.actions_funcs['block'] = lambda meta, params: (entered.set(), release.wait(3))
    engine.actions_meta['block'] = {'params': [], 'outputs': []}
    blocking = rule([{'type': 'block', 'binding_id': 'a_block01', 'params': {}}], [edge('a_block01', None)])
    blocking['rule_id'] = 'r_block001'
    try:
        manager.submit('block', blocking, '占用执行线程', build_context('占用', 'start', {}, [], 'r_block001', 'run_block'))
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
        with pytest.raises(ValueError, match='旧条件模型已移除'):
            normalize_workflow_rule(old)


def test_nested_entry_event_group_can_start_outer_action_groups():
    from notmyfault.tests.api_support import create_test_engine
    from notmyfault.core.conditions import get_rule_condition
    decision = group('a_entry01', [{'op': 'event_occurred', 'trigger_id': 't_start01'}, None], activation=leaf())
    inside = {'entry': 'a_entry01', 'nodes': [decision, action('a_first01', 'first')], 'edges': [
        edge('a_entry01', 'a_first01', decision['branches'][0]['id']), edge('a_entry01', None, decision['branches'][1]['id']), edge('a_first01', None)]}
    inner = {'type': 'action_group', 'binding_id': 'a_inner01', 'workflow': inside}
    outer = {'type': 'action_group', 'binding_id': 'a_outer01', 'workflow': {'entry': 'a_inner01', 'nodes': [inner], 'edges': [edge('a_inner01', None)]}}
    source = rule([outer, action('a_after01', 'after')], [edge('a_outer01', 'a_after01'), edge('a_after01', None)])
    source.pop('_matcher')
    source = canvas_fixture(source, internal_start=True)
    from notmyfault.core.canvas_workflow import lower_canvas_rule
    assert get_rule_condition(lower_canvas_rule(source)) == leaf()
    calls = []
    engine = create_test_engine({'rules': [source]})
    engine.triggers_meta.update(TRIGGERS)
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params: calls.append(params['label'])
    try:
        engine.emit_event('start', {'value': 1})
        assert engine._run_manager.wait_for_idle(3)
        assert calls == ['first', 'after']
    finally:
        engine.shutdown(3)


def _download_graph():
    ref = lambda identity, field: {'$ref': {'scope': 'step', 'node': identity, 'path': [field]}}
    limit = {'$ref': {'scope': 'variable', 'node': 'v_limit01', 'path': []}}
    event = lambda identity: {'op': 'event_occurred', 'trigger_id': identity}
    state = lambda identity, value: {'type': 'set_variable', 'binding_id': identity, 'variable': 'v_status1', 'value': value}
    speed = lambda identity, phase: {'type': 'speed', 'binding_id': identity, 'params': {'phase': phase}}
    sequence = lambda nodes: {'entry': nodes[0]['binding_id'], 'nodes': nodes, 'edges': [edge(node['binding_id'], nodes[index + 1]['binding_id'] if index + 1 < len(nodes) else None) for index, node in enumerate(nodes)]}

    def choice(decision, flows):
        nodes, edges = [decision], []
        for exit_branch, flow in zip(decision['branches'], flows):
            nodes.extend(flow)
            edges.append(edge(decision['binding_id'], flow[0]['binding_id'] if flow else None, exit_branch['id']))
            edges.extend(edge(node['binding_id'], flow[index + 1]['binding_id'] if index + 1 < len(flow) else None) for index, node in enumerate(flow))
        return {'entry': decision['binding_id'], 'nodes': nodes, 'edges': edges}

    def prepend(prefix, graph):
        return {'entry': prefix[0]['binding_id'], 'nodes': prefix + graph['nodes'], 'edges':
            [edge(node['binding_id'], prefix[index + 1]['binding_id'] if index + 1 < len(prefix) else graph['entry']) for index, node in enumerate(prefix)] + graph['edges']}

    def resume(identity):
        speed_id = f'a_{identity}speed'
        predicate = {'op': 'all', 'children': [
            {'op': 'is_true', 'left': ref(speed_id, 'connected')}, {'op': 'lte', 'left': ref(speed_id, 'latency'), 'right': limit}]}
        decide = group(f'a_{identity}safe', [predicate, None])
        graph = choice(decide, [[state(f'a_{identity}set1', 'running'), action(f'a_{identity}note1', 'resume')],
                                [state(f'a_{identity}set0', 'paused'), action(f'a_{identity}note0', 'keep_paused')]])
        return {'type': 'action_group', 'binding_id': f'a_{identity}group', 'workflow': prepend([speed(speed_id, 'repair')], graph)}

    quality = group('a_quality1', [{'op': 'all', 'children': [
        {'op': 'is_true', 'left': ref('a_recheck1', 'connected')},
        {'op': 'any', 'children': [{'op': 'lte', 'left': ref('a_recheck1', 'latency'), 'right': limit},
                                  {'op': 'lt', 'left': ref('a_recheck1', 'latency'), 'right': ref('a_measure1', 'latency')}]}]}, None])
    repair = {'type': 'action_group', 'binding_id': 'a_repair01', 'workflow': prepend([
        action('a_relink1', 'reconnect'), speed('a_recheck1', 'repair')], choice(quality, [[action('a_good001', 'repair_ok')], [action('a_bad0001', 'repair_failed')]]))}
    needs_repair = group('a_needfix1', [{'op': 'is_false', 'left': ref('a_measure1', 'connected')}, None])
    protection = {'type': 'action_group', 'binding_id': 'a_protect1', 'workflow': prepend([
        action('a_capture1', 'capture'), state('a_pause001', 'paused')], choice(needs_repair, [[repair], [action('a_skipfix1', 'skip_repair')]]))}
    steam_start, steam_exit, network = leaf('steam', 't_steam01'), leaf('steam', 't_exit001'), leaf('network', 't_net001')
    steam_start['params'] = {'value': 1}
    steam_exit['params'] = {'value': 0}
    network['params'] = {'value': 1}
    protection_condition = {'op': 'any', 'children': [
        {'op': 'all', 'children': [event('t_steam01'), {'op': 'run_status', 'rule_id': 'r_graph001', 'status': 'running'},
            {'op': 'not', 'children': [{'op': 'lte', 'left': ref('a_measure1', 'latency'), 'right': limit}]}]},
        {'op': 'is_false', 'left': ref('a_measure1', 'connected')}]}
    resume_condition = {'op': 'all', 'children': [{'op': 'any', 'children': [event('t_exit001'), event('t_net001')]},
        {'op': 'is_true', 'left': ref('a_measure1', 'connected')}]}
    dispatch = group('a_dispatch', [protection_condition, resume_condition, None], triggers=[steam_start, steam_exit, network])
    wait = group('a_recover1', [event('t_exit001'), {'op': 'all', 'children': [event('t_net001'), {'op': 'not', 'children': [event('t_exit001')]}]}, None],
        mode='wait', activation={'op': 'any', 'children': [steam_exit, network]})
    recovery, direct = resume('recover'), resume('direct1')
    graph = rule([speed('a_measure1', 'first'), dispatch, protection, wait, recovery, direct,
        action('a_hold001', 'hold_paused'), action('a_unknown1', 'uncertain'), action('a_idle001', 'idle')], [
        edge('a_measure1', 'a_dispatch'), edge('a_dispatch', 'a_protect1', dispatch['branches'][0]['id']),
        edge('a_dispatch', direct['binding_id'], dispatch['branches'][1]['id']), edge('a_dispatch', 'a_idle001', dispatch['branches'][2]['id']),
        edge('a_protect1', 'a_recover1'), edge('a_recover1', recovery['binding_id'], wait['branches'][0]['id']),
        edge('a_recover1', 'a_hold001', wait['branches'][1]['id']), edge('a_recover1', 'a_unknown1', wait['branches'][2]['id']),
        edge(recovery['binding_id'], None), edge(direct['binding_id'], None), edge('a_hold001', None), edge('a_unknown1', None), edge('a_idle001', None)])
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
    executor._actions_funcs()['speed'] = lambda meta, params: {'connected': (initial if params['phase'] == 'first' else repair)[0], 'latency': (initial if params['phase'] == 'first' else repair)[1]}
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
    source = rule([action('a_first01', 'first')], [edge('a_first01', None)])
    if field == 'entry':
        source['workflow'][field] = value
    else:
        source['workflow']['edges'][0][field] = value
    assert validate_runtime_structure(source)
    with pytest.raises(ValueError):
        normalize_rule_input([source])


@pytest.mark.parametrize('field, value', [('root', []), ('root', {}), ('source', {}), ('target', [])])
def test_malformed_condition_identifiers_raise_structured_validation_error(field, value):
    graph = predicate_condition_graph({'op': 'not', 'children': [{'op': 'is_true', 'left': True}]})
    if field == 'root':
        graph[field] = value
    else:
        graph['edges'][0][field] = value
    with pytest.raises(ValueError):
        condition_graph_predicate(graph)


def test_export_type_inference_understands_literal_template_and_conversion():
    from notmyfault.core.binding_schema import rule_binding_types
    exports = {'literal': {'$literal': 'text'}, 'template': {'$template': ['prefix', 'suffix']},
               'converted': {'$convert': {'value': '3', 'to': 'number'}},
               'nested': {'values': [{'$literal': True}, {'$literal': False}]}}
    outer = {'type': 'action_group', 'binding_id': 'a_outer01', 'workflow': {'entry': None, 'nodes': [], 'edges': []}, 'exports': exports}
    source = rule([outer], [edge('a_outer01', None)])
    schema = rule_binding_types(source, TRIGGERS, ACTIONS)['step']['a_outer01']['properties']
    assert schema['literal']['type'] == 'text'
    assert schema['template']['type'] == 'text'
    assert schema['converted']['type'] == 'number'
    assert schema['nested']['properties']['values']['items']['type'] == 'bool'


def test_late_sensitive_event_remains_masked_through_assignment_and_group_export():
    from notmyfault.tests.api_support import create_test_engine, event_fields
    from notmyfault.values import contains_secret
    waited = group('a_wait001', [None], mode='wait', activation=leaf('steam', 't_steam01'))
    assignment = {'type': 'set_variable', 'binding_id': 'a_assign1', 'variable': 'v_secret1',
                  'value': {'$ref': {'scope': 'trigger', 'node': 't_steam01', 'path': ['token']}}}
    outer = {'type': 'action_group', 'binding_id': 'a_outer01', 'workflow': {'entry': 'a_enter01', 'nodes': [{'type': 'merge', 'binding_id': 'a_enter01'}, waited, assignment], 'edges': [
        edge('a_enter01', 'a_wait001'), edge('a_wait001', 'a_assign1', waited['branches'][0]['id']), edge('a_assign1', None)]},
        'exports': {'token': {'$ref': {'scope': 'variable', 'node': 'v_secret1', 'path': []}}}}
    output = {'type': 'record', 'binding_id': 'a_record1', 'params': {'label': {'$ref': {'scope': 'step', 'node': 'a_outer01', 'path': ['token']}}}}
    source = rule([outer, output], [edge('a_outer01', 'a_record1'), edge('a_record1', None)])
    source['variables'] = [{'id': 'v_secret1', 'name': '令牌', 'value_type': 'text'}]
    source = canvas_fixture(source)
    packets, calls, contexts = [], [], []
    engine = create_test_engine({'rules': [source]}, on_event=lambda event: packets.append(event.to_dict()))
    engine.triggers_meta.update(TRIGGERS)
    engine.triggers_meta['steam'] = {'params': [], 'outputs': [{'name': 'token', 'type': 'text', 'sensitive': True}]}
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params: calls.append(str(params['label']))
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
    producer = {'type': 'action_group', 'binding_id': 'a_first01', 'workflow': {'entry': None, 'nodes': [], 'edges': []}, 'exports': {'value': {'$literal': 12}}}
    consumer = {'type': 'action_group', 'binding_id': 'a_second1', 'workflow': {'entry': None, 'nodes': [], 'edges': []},
                'exports': {'value': {'$ref': {'scope': 'step', 'node': 'a_first01', 'path': ['value']}}}}
    source = rule([consumer, producer], [edge('a_first01', 'a_second1'), edge('a_second1', None)], 'a_first01')
    schemas = rule_binding_types(source, TRIGGERS, ACTIONS)['step']
    assert schemas['a_second1']['properties']['value']['type'] == schemas['a_first01']['properties']['value']['type']
    assert schemas['a_second1']['properties']['value']['type'] in ('int', 'number')
    assert validate_runtime_bindings(source, TRIGGERS, ACTIONS) == []


def test_group_export_failure_records_failed_step_and_closed_generator():
    outer = {'type': 'action_group', 'binding_id': 'a_outer01', 'workflow': {'entry': None, 'nodes': [], 'edges': []},
             'exports': {'value': {'$ref': {'scope': 'variable', 'node': 'v_uninit1', 'path': [], 'on_missing': 'error'}}}}
    source = rule([outer], [edge('a_outer01', None)])
    source['variables'] = [{'id': 'v_uninit1', 'name': '未初始化', 'value_type': 'text'}]
    source, context, observer, executor, records, events = setup(source)
    assert executor.execute_workflow('one', source, source['name'], context).status == 'failed'
    assert context['steps']['a_outer01']['status'] == 'failed'
    assert any(kind == 'run.step.failed' and data['step_id'] == 'a_outer01' for kind, data in events)
    assert '_graph_execution' not in context


def test_nested_cancellation_records_all_open_groups_and_clears_execution_position():
    outer = {'type': 'action_group', 'binding_id': 'a_outer01', 'workflow': waiting_rule()['workflow']}
    source = rule([outer], [edge('a_outer01', None)])
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
    producer = {'type': 'speed', 'binding_id': 'a_speed01', 'params': {}}
    assignment = {'type': 'set_variable', 'binding_id': 'a_assign1', 'variable': 'v_result1',
                  'value': {'$ref': {'scope': 'step', 'node': 'a_speed01', 'path': ['latency']}}}
    inner = {'type': 'action_group', 'binding_id': 'a_inner01', 'workflow': {'entry': 'a_assign1', 'nodes': [assignment], 'edges': [edge('a_assign1', None)]}}
    source = rule([producer, inner], [edge('a_speed01', 'a_inner01'), edge('a_inner01', None)])
    source['variables'] = [{'id': 'v_result1', 'name': '延迟', 'value_type': 'number', 'initial': 0}]
    source, context, observer, executor, records, events = setup(source)
    scope = find_graph_scope(source['workflow'], 'a_assign1')
    assert scope['available'] == {'a_speed01'}
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
    choose = group('a_choice1', [{'op': 'is_true', 'left': True}, None])
    if kind == 'action_group':
        producer = {'type': kind, 'binding_id': 'a_source1', 'workflow': {'entry': None, 'nodes': [], 'edges': []},
                    'exports': {'value': {'$ref': {'scope': 'variable', 'node': 'v_secret1', 'path': []}}}}
    else:
        producer = {'type': kind, 'binding_id': 'a_source1', 'variable': 'v_secret1', 'value': 'initial-secret'}
    target = action('a_target1', {'$ref': {'scope': 'step', 'node': 'a_source1', 'path': ['value'], 'on_missing': 'default', 'default': 'missing'}})
    inner = {'type': 'action_group', 'binding_id': 'a_inner01', 'workflow': {'entry': 'a_target1', 'nodes': [target], 'edges': [edge('a_target1', None)]}}
    source = rule([choose, producer, inner], [edge('a_choice1', 'a_source1', choose['branches'][0]['id']),
                  edge('a_choice1', 'a_inner01', choose['branches'][1]['id']), edge('a_source1', 'a_inner01'), edge('a_inner01', None)])
    source['variables'] = [{'id': 'v_secret1', 'name': '令牌', 'value_type': 'text', 'sensitive': True, 'initial': 'initial-secret'}]
    scope = find_graph_scope(source['workflow'], 'a_target1')
    assert scope['available'] == {'a_choice1'}
    assert scope['possible'] == {'a_choice1', 'a_source1'}
    assert validate_runtime_bindings(source, TRIGGERS, ACTIONS) == []
    source = canvas_fixture(source)
    calls, packets, contexts = [], [], []
    engine = create_test_engine({'rules': []}, on_event=lambda event: packets.append(event.to_dict()))
    engine.triggers_meta.update(TRIGGERS)
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params: calls.append(str(params['label']))
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
    choose = group('a_choice1', [{'op': 'is_true', 'left': False}, None])
    speed = {'type': 'speed', 'binding_id': 'a_speed01', 'params': {}}
    nodes = [choose, speed]
    else_target = None
    if termination == 'end':
        nodes.append({'type': 'end', 'binding_id': 'a_end0001'})
        else_target = 'a_end0001'
    reference = {'scope': 'step', 'node': 'a_speed01', 'path': ['latency']}
    if policy:
        reference['on_missing'] = policy
    if policy == 'default':
        reference['default'] = 123
    outer = {'type': 'action_group', 'binding_id': 'a_outer01',
        'workflow': {'entry': 'a_choice1', 'nodes': nodes, 'edges': [
            edge('a_choice1', 'a_speed01', choose['branches'][0]['id']),
            edge('a_choice1', else_target, choose['branches'][1]['id']), edge('a_speed01', None)]},
        'exports': {'latency': {'$ref': reference}}}
    source = rule([outer, action('a_after01', 'after')], [edge('a_outer01', 'a_after01'), edge('a_after01', None)])
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
    node = group('a_group01', [None]) if kind == 'event_group' else {
        'type': kind, 'binding_id': 'a_group01', 'workflow': {'entry': None, 'nodes': [], 'edges': []}}
    node[field] = {'params': {}, 'retry': 1, 'retry_delay_seconds': 1, 'retry_backoff': 2, 'failure_actions': [], 'timeout_seconds': 1}[field]
    source = rule([node], [edge('a_group01', None, node['branches'][0]['id'] if kind == 'event_group' else 'next')])
    assert any(field in error for error in validate_runtime_structure(source))


def test_event_group_rejects_error_continuation_and_timeout_stays_failed():
    source = waiting_rule(timeout=1)
    waited = next(node for node in source['workflow']['nodes'] if node['type'] == 'event_group')
    waited['on_error'] = 'continue'
    assert any('on_error:continue' in error for error in validate_runtime_structure(source))
    waited['on_error'] = 'stop'
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
    source = canvas_fixture(waiting_rule())
    engine = create_test_engine({'rules': []}, on_event=lambda event: packets.append(event.to_dict()))
    engine.triggers_meta.update(TRIGGERS)
    engine.actions_meta.update(ACTIONS)
    engine.actions_funcs['record'] = lambda meta, params: calls.append(params['label'])
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
    choose = group('a_choice1', [{'op': 'all', 'children': [{'op': 'is_true', 'left': first}, {'op': 'is_true', 'left': True}]},
                                 {'op': 'is_true', 'left': True}, None])
    source = rule([choose, action('a_after01', 'after')], [
        *(edge('a_choice1', 'a_after01', branch['id']) for branch in choose['branches']), edge('a_after01', None)])
    source, context, observer, executor, records, events = setup(source)
    assert executor.execute_workflow('one', source, source['name'], context).status == 'succeeded'
    selected = next(data for kind, data in events if kind == 'run.group.selected')
    evaluated = selected['evaluated_branches']
    assert [(item['branch_id'], item['result']) for item in evaluated] == (
        [(choose['branches'][0]['id'], True)] if first else [(choose['branches'][0]['id'], False), (choose['branches'][1]['id'], True)])
    assert len(evaluated[0]['predicates']) == (3 if first else 2)
    assert [item['path'] for item in evaluated[0]['predicates']] == (
        ['condition.children[0]', 'condition.children[1]', 'condition'] if first else ['condition.children[0]', 'condition'])
    assert all(item.get('node_id') for record in evaluated for item in record['predicates'])
    history = RunHistory(str(tmp_path / 'runs.jsonl'))
    history.record({'type': 'run.triggered', 'data': {'run_id': 'run_graph'}})
    for kind, data in events:
        history.record({'type': kind, 'data': data})
    history.flush()
    saved = RunHistory(history.path).get_run('run_graph')['steps'][0]
    assert saved['evaluated_branches'] == evaluated
    assert saved['branch'] == choose['branches'][0 if first else 1]['id']


def test_group_condition_error_is_recorded_without_false_or_else_selection(tmp_path):
    from notmyfault.core.run_history import RunHistory
    choose = group('a_choice1', [{'op': 'is_true', 'left': False},
        {'op': 'eq', 'left': {'$ref': {'scope': 'trigger', 'node': 't_steam01', 'path': ['value']}}, 'right': 1}, None], triggers=[leaf('steam', 't_steam01')])
    source = rule([choose], [edge('a_choice1', None, branch['id']) for branch in choose['branches']])
    source, context, observer, executor, records, events = setup(source)
    assert executor.execute_workflow('one', source, source['name'], context).status == 'failed'
    assert not any(kind == 'run.group.selected' for kind, data in events)
    failed = next(data for kind, data in events if kind == 'run.step.failed')
    evaluated = failed['evaluated_branches']
    assert evaluated[0]['result'] is False
    assert 'result' not in evaluated[1] and evaluated[1]['error']['code'] == 'missing_binding_value'
    assert 'result' not in evaluated[1]['predicates'][0]
    history = RunHistory(str(tmp_path / 'runs.jsonl'))
    history.record({'type': 'run.triggered', 'data': {'run_id': 'run_graph'}})
    for kind, data in events:
        history.record({'type': kind, 'data': data})
    history.flush()
    restored = RunHistory(history.path).get_run('run_graph')['steps'][0]
    assert restored['evaluated_branches'] == evaluated and 'branch' not in restored
