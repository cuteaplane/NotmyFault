import copy
import threading
from types import SimpleNamespace

import pytest

from notmyfault.core.clock import FakeClock
from notmyfault.core.condition_runtime import ConditionRuntime
from notmyfault.core.diagnostics import Diagnostics
from notmyfault.core.runtime_rules import prepare_rules
from notmyfault.core.workflow import build_context
from notmyfault.core.workflow_executor import WorkflowExecutor
from notmyfault.core.rules import validate_rule_structure


def graph_rule(*, continuation='stop', recovery=True, recovery_fails=False):
    nodes = [
        {'binding_id': 'a_source0001', 'type': 'trigger', 'trigger': {'binding_id': 't_source0001', 'type': 'signal', 'params': {}}},
        {'binding_id': 'a_task000001', 'type': 'task', 'params': {}, 'on_error': continuation},
        {'binding_id': 'a_after00001', 'type': 'after', 'params': {}},
    ]
    edges = [
        {'source': 'a_source0001', 'port': 'event', 'target': 'a_task000001', 'channel': 'control'},
        {'source': 'a_task000001', 'port': 'next', 'target': 'a_after00001', 'channel': 'control'},
    ]
    if recovery:
        nodes.extend([
            {'binding_id': 'a_recovery01', 'type': 'action_group', 'name': '补救'},
            {'binding_id': 'a_repair0001', 'parent': 'a_recovery01', 'type': 'repair', 'params': {}, 'on_error': 'continue'},
        ])
        edges.extend([
            {'source': 'a_task000001', 'port': 'failure', 'target': 'a_recovery01', 'channel': 'control'},
            {'source': 'a_recovery01', 'port': 'entry', 'target': 'a_repair0001', 'channel': 'control'},
        ])
    return {'rule_id': 'r_direct0001', 'name': '补救图', 'workflow': {'entry': None, 'nodes': nodes, 'edges': edges}}


def execute(rule, calls, *, repair_fails=False):
    def task(_meta, _params, _context):
        calls.append('task')
        raise RuntimeError('任务失败')
    def repair(_meta, _params, _context):
        calls.append('repair')
        if repair_fails:
            raise RuntimeError('补救失败')
        return {}
    def after(_meta, _params, _context):
        calls.append('after')
        return {}
    metadata = {name: {'execution_api': 'context-v1'} for name in ('task', 'repair', 'after')}
    prepared = prepare_rules([rule], {'signal': {}}, metadata, validate=False)
    compiled = prepared.compiled[rule['rule_id']]
    events = prepared.runtime.match_and_take(rule['rule_id'], compiled.source, 'signal', {})
    context = build_context(rule['name'], 'signal', {}, events, rule['rule_id'], 'run_direct')
    prepared.prepare_context(compiled.source, context)
    context['_entry_root'] = events[0]['root_id']
    context['_entry_choices'] = {'a_source0001': 'event'}
    emitted = []
    executor = WorkflowExecutor(actions_meta=lambda: metadata, actions_funcs=lambda: {'task': task, 'repair': repair, 'after': after},
        plugin_modules=lambda: {name: SimpleNamespace(run_with_context=runner) for name, runner in {'task': task, 'repair': repair, 'after': after}.items()},
        diagnostics=Diagnostics(), shutdown_event=lambda: threading.Event(),
        on_event=lambda kind, data: emitted.append((kind, data)))
    result = executor.execute_workflow(rule['rule_id'], compiled.source, rule['name'], context)
    return result, context, emitted


@pytest.mark.parametrize('continuation,handler,broken,status,expected', [
    ('stop', True, False, 'recovered', ['task', 'repair']),
    ('continue', True, False, 'succeeded', ['task', 'repair', 'after']),
    ('continue', True, True, 'failed', ['task', 'repair']),
    ('continue', False, False, 'succeeded', ['task', 'after']),
    ('stop', False, False, 'failed', ['task']),
])
def test_recovery_returns_follow_graph_and_keep_failed_step(continuation, handler, broken, status, expected):
    calls = []
    rule = graph_rule(continuation=continuation, recovery=handler)
    assert validate_rule_structure(rule) == []
    outcome, context, events = execute(rule, calls, repair_fails=broken)
    assert outcome.status == status
    assert calls == expected
    assert context['steps']['a_task000001']['status'] == 'failed'
    if handler:
        assert any(kind == 'run.recovery.entered' for kind, _data in events)
        assert any(kind == ('run.recovery.failed' if broken else 'run.recovery.completed') for kind, _data in events)


def test_recovery_group_rejects_parallel_main_entry_and_external_next():
    rule = graph_rule()
    rule['workflow']['edges'].append({'source': 'a_after00001', 'port': 'next', 'target': 'a_recovery01', 'channel': 'control'})
    assert any('主流程入口' in error for error in validate_rule_structure(rule))
    rule = graph_rule()
    rule['workflow']['edges'].append({'source': 'a_recovery01', 'port': 'next', 'target': 'a_after00001', 'channel': 'control'})
    assert any('对外 next' in error for error in validate_rule_structure(rule))


def test_compiler_keeps_flat_source_and_node_order_does_not_change_execution():
    original = graph_rule(continuation='continue')
    reversed_rule = copy.deepcopy(original)
    reversed_rule['workflow']['nodes'].reverse()
    first, second = [], []
    left, _ctx, _events = execute(original, first)
    right, _ctx, _events = execute(reversed_rule, second)
    assert (left.status, first) == (right.status, second)
    prepared = prepare_rules([original], {'signal': {}}, {name: {} for name in ('task', 'repair', 'after')}, validate=False)
    compiled = prepared.compiled[original['rule_id']]
    assert not hasattr(compiled, 'plan')
    assert not hasattr(compiled.matcher_spec, 'node')
    assert len(compiled.source['workflow']['nodes']) == len(original['workflow']['nodes'])
    assert all('condition_graph' not in node and 'workflow' not in node for node in compiled.source['workflow']['nodes'])
    with pytest.raises(TypeError):
        compiled.matcher_spec.graph.nodes = {}


def test_direct_absence_timer_is_reset_by_event():
    rule = graph_rule(recovery=False)
    rule['workflow']['nodes'][0].update(occurrence='absent', duration_seconds=3, repeat='every_window')
    clock = FakeClock()
    runtime = ConditionRuntime(clock=clock)
    runtime.register_rule(rule['rule_id'], rule)
    assert runtime.take_due(2) == []
    assert not runtime.match(rule['rule_id'], rule, 'signal', {}, now=2)
    assert runtime.take_due(4) == []
    due = runtime.take_due(5)
    assert len(due) == 1 and due[0][1][0]['absence'] is True
    assert not runtime.snapshot()[rule['rule_id']].seen
    assert runtime.next_deadline() == 8


def test_recovery_history_preserves_original_failure_and_handler_result(tmp_path):
    from notmyfault.core.run_history import RunHistory
    rule = graph_rule()
    outcome, context, emitted = execute(rule, [])
    history = RunHistory(str(tmp_path / 'runs.jsonl'))
    for timestamp, (kind, data) in enumerate(emitted):
        history.record_async({'v': 1, 'type': kind, 'data': data, 'ts': timestamp})
    history.record_async({'v': 1, 'type': 'run.' + outcome.status, 'data': {'run_id': 'run_direct', 'rule_id': rule['rule_id']}, 'ts': len(emitted)})
    history.flush()
    restored = RunHistory(history.path).get_run('run_direct')
    assert restored['status'] == 'recovered'
    failed = next(step for step in restored['steps'] if step['step_id'] == 'a_task000001')
    assert failed['status'] == 'failed'
    assert failed['recovery']['group_id'] == 'a_recovery01'
    assert failed['recovery']['status'] == 'succeeded'
