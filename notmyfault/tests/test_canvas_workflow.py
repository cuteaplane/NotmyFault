from types import SimpleNamespace

import pytest

from notmyfault.core.canvas_workflow import CanvasProgram, validate_canvas_graph
from notmyfault.core.event_observation import RunEventObservation
from notmyfault.core.predicates import evaluate_predicate
from notmyfault.core.rule_model import normalize_rule_shape
from notmyfault.core.rules import normalize_rule_input
from notmyfault.core.workflow_graph import evaluate_group_branches, execution_nodes
from notmyfault.core.condition_program import build_matcher


def trigger(suffix):
    return {'binding_id': 'a_source_' + suffix, 'type': 'trigger', 'trigger': {'binding_id': 't_source_' + suffix, 'type': 'event_' + suffix, 'params': {}}}


def example():
    a, b = trigger('aaaaaa'), trigger('bbbbbb')
    group = {'binding_id': 'a_choice_000001', 'type': 'event_group', 'group_type': 'if', 'branches': [{'id': 'b_first001'}, {'id': 'b_second01'}, {'id': 'b_spare001'}, {'id': 'b_other001', 'else': True}]}
    actions = [{'binding_id': 'a_action_' + suffix, 'type': 'record', 'params': {'message': suffix}} for suffix in ('aaaaaa', 'bbbbbb')]
    edges = [
        {'source': a['binding_id'], 'port': 'event', 'target': group['binding_id'], 'target_port': 'b_first001', 'channel': 'condition'},
        {'source': b['binding_id'], 'port': 'event', 'target': group['binding_id'], 'target_port': 'b_second01', 'channel': 'condition'},
        {'source': group['binding_id'], 'port': 'b_first001', 'target': actions[0]['binding_id'], 'channel': 'control'},
        {'source': group['binding_id'], 'port': 'b_second01', 'target': actions[1]['binding_id'], 'channel': 'control'},
    ]
    return {'rule_id': 'r_canvas_000001', 'name': '双事件分支', 'workflow': {'entry': None, 'nodes': [a, b, group, *actions], 'edges': edges}}


def test_elif_starts_without_waiting_for_if_and_spare_branch_is_not_evaluated():
    source = example()
    assert validate_canvas_graph(source['workflow']) == []
    program = CanvasProgram(source)
    group = program.nodes['a_choice_000001']
    assert program.roots() == ('a_choice_000001',)
    assert [branch['id'] for branch in group['branches']] == ['b_first001', 'b_second01', 'b_spare001', 'b_other001']
    evaluated = []
    assert evaluate_group_branches(group, {}, evaluated, program=program, status_lookup=None, occurred={'t_source_bbbbbb'}) == 'b_second01'
    assert [item['result'] for item in evaluated] == [False, True]
    assert source == example()


def test_if_wins_when_both_events_have_arrived():
    program = CanvasProgram(example())
    group = program.nodes['a_choice_000001']
    evaluated = []
    assert evaluate_group_branches(group, {}, evaluated, program=program, status_lookup=None, occurred={'t_source_aaaaaa', 't_source_bbbbbb'}) == 'b_first001'
    assert len(evaluated) == 1


def test_disconnected_blocks_are_saved_but_not_executed():
    source = example()
    unused = trigger('cccccc')
    source['workflow']['nodes'] += [unused, {'type': 'record', 'binding_id': 'a_unused_000001', 'params': {}}]
    assert normalize_rule_input([source])[0]['workflow']['nodes'][-2] == unused
    assert 'a_unused_000001' not in {node['binding_id'] for node, _path in execution_nodes(source['workflow'])}
    assert 'event_cccccc' not in build_matcher(source).leaves
    assert CanvasProgram(source).nodes['a_unused_000001'] == source['workflow']['nodes'][-1]


def test_unwired_group_can_be_saved_without_inventing_conditions():
    source = example()
    source['workflow']['edges'] = []
    assert normalize_rule_input([source])[0]['workflow']['edges'] == []
    assert CanvasProgram(source).roots() == ()
    assert list(execution_nodes(source['workflow'])) == []


@pytest.mark.parametrize('obsolete', ['condition', 'event', 'trigger', 'actions', 'preconditions'])
def test_old_rule_fields_are_rejected(obsolete):
    source = example()
    source[obsolete] = {}
    with pytest.raises(ValueError, match='旧条件模型已移除'):
        normalize_rule_shape(source)


def test_condition_cannot_be_connected_to_else():
    source = example()
    source['workflow']['edges'][0]['target_port'] = 'b_other001'
    assert any('ELSE' in error for error in validate_canvas_graph(source['workflow']))


def test_connected_if_requires_explicit_logic_for_multiple_inputs():
    source = example()
    source['workflow']['edges'][1]['target_port'] = 'b_first001'
    assert any('先组合 AND 或 OR' in error for error in validate_canvas_graph(source['workflow']))


def test_group_containment_cycle_and_graph_cycle_are_rejected():
    source = example()
    source['workflow']['nodes'][2]['parent'] = 'a_choice_000001'
    assert any('祖先组' in error for error in validate_canvas_graph(source['workflow']))
    source = example()
    source['workflow']['edges'].append({'source': 'a_action_aaaaaa', 'port': 'next', 'target': 'a_choice_000001', 'channel': 'control'})
    assert any('循环' in error for error in validate_canvas_graph(source['workflow']))


def test_timed_absence_is_false_before_duration_and_resets_on_event():
    class Clock:
        time = 0
        def monotonic(self):
            return self.time
    clock = Clock()
    leaf = trigger('aaaaaa')['trigger']
    compiled = SimpleNamespace(source={'workflow': {'nodes': [{'binding_id': 'a_trigger_000001', 'type': 'trigger', 'trigger': leaf}], 'edges': []}})
    observation = RunEventObservation(compiled, clock=clock)
    node = compiled.source['workflow']['nodes'][0]
    node.update(occurrence='absent', absence_scope='window', duration_seconds=10)
    program = CanvasProgram(compiled.source)
    context = {'_event_observation': observation}
    clock.time = 5
    assert program.evaluate(node['binding_id'], context) is False
    clock.time = 10
    assert program.evaluate(node['binding_id'], context) is True
    observation.record(leaf['type'], {})
    clock.time = 15
    assert program.evaluate(node['binding_id'], context) is False
    clock.time = 20
    assert program.evaluate(node['binding_id'], context) is True
    assert observation._waits == {}


def test_and_and_nested_not_are_derived_from_explicit_connections():
    source = example()
    logic = {'binding_id': 'a_logic_000001', 'type': 'logic', 'op': 'all'}
    negation = {'binding_id': 'a_logic_000002', 'type': 'logic', 'op': 'not'}
    source['workflow']['nodes'] += [logic, negation]
    source['workflow']['edges'][0].update(target=logic['binding_id'], target_port='input')
    source['workflow']['edges'][1].update(target=negation['binding_id'], target_port='input')
    source['workflow']['edges'] += [
        {'source': negation['binding_id'], 'port': 'result', 'target': logic['binding_id'], 'channel': 'condition'},
        {'source': logic['binding_id'], 'port': 'result', 'target': 'a_choice_000001', 'target_port': 'b_first001', 'channel': 'condition'},
    ]
    source['workflow']['edges'] = [edge for edge in source['workflow']['edges'] if edge['port'] != 'b_second01']
    assert validate_canvas_graph(source['workflow']) == []
    program = CanvasProgram(source)
    assert program.evaluate(logic['binding_id'], {}, occurred={'t_source_aaaaaa'}) is True
    assert program.evaluate(logic['binding_id'], {}, occurred={'t_source_aaaaaa', 't_source_bbbbbb'}) is False
    assert program.evaluate(logic['binding_id'], {}, occurred=set()) is False


def test_nested_action_group_uses_explicit_entry_and_preserves_step_identity():
    source = example()
    group = {'binding_id': 'a_actions_000001', 'type': 'action_group', 'exports': {'copied': {'$ref': {'scope': 'step', 'node': 'a_action_aaaaaa', 'path': ['value']}}}}
    source['workflow']['nodes'].append(group)
    source['workflow']['nodes'][3]['parent'] = group['binding_id']
    source['workflow']['edges'][2]['target'] = group['binding_id']
    source['workflow']['edges'].append({'source': group['binding_id'], 'port': 'entry', 'target': 'a_action_aaaaaa', 'channel': 'control'})
    program = CanvasProgram(source)
    assert program.nodes['a_action_aaaaaa']['parent'] == group['binding_id']
    assert program.successors[group['binding_id'], 'entry'] == 'a_action_aaaaaa'
    assert 'workflow' not in program.nodes[group['binding_id']]


def test_empty_unused_trigger_does_not_block_engine_preparation():
    from notmyfault.core.runtime_rules import prepare_rules
    source = example()
    source['workflow']['edges'] = []
    prepared = prepare_rules([source], {'event_aaaaaa': {'params': [{'name': 'path', 'value_type': 'path', 'required': True}]}}, {})
    assert len(prepared) == 1
    assert prepared.issues == []
    assert prepared.trigger_params == {}


def test_branch_negation_does_not_negate_sibling_branches():
    source = example()
    source['workflow']['nodes'][2]['branches'][0]['negated'] = True
    program = CanvasProgram(source)
    group = program.nodes['a_choice_000001']
    evaluated = []
    assert evaluate_group_branches(group, {}, evaluated, program=program, status_lookup=None, occurred={'t_source_aaaaaa', 't_source_bbbbbb'}) == 'b_second01'
    assert [item['result'] for item in evaluated] == [False, True]


def test_nested_elif_output_is_false_when_prior_if_wins():
    source = example()
    program = CanvasProgram(source)
    both = {'t_source_aaaaaa', 't_source_bbbbbb'}
    assert program.evaluate('a_choice_000001', {}, port='b_second01', occurred=both) is False
    assert program.evaluate('a_choice_000001', {}, port='b_second01', occurred={'t_source_bbbbbb'}) is True
    assert program.evaluate('a_choice_000001', {}, port='b_other001', occurred=set()) is True


def test_branch_output_keeps_order_when_used_as_nested_condition():
    source = example()
    outer = {'binding_id': 'a_logic_outer01', 'type': 'logic', 'op': 'any'}
    source['workflow']['nodes'].append(outer)
    source['workflow']['edges'].append({'source': 'a_choice_000001', 'port': 'b_second01', 'target': outer['binding_id'], 'channel': 'condition'})
    program = CanvasProgram(source)
    assert program.evaluate(outer['binding_id'], {}, occurred={'t_source_aaaaaa', 't_source_bbbbbb'}) is False
    assert program.evaluate(outer['binding_id'], {}, occurred={'t_source_bbbbbb'}) is True


@pytest.mark.parametrize('port', ['missing', 'next', 'entry'])
def test_invalid_trigger_source_port_is_rejected(port):
    source = example()
    source['workflow']['edges'][0]['port'] = port
    assert any('来源端口' in error for error in validate_canvas_graph(source['workflow']))


def test_control_flow_cannot_skip_action_group_boundary():
    source = example()
    group = {'binding_id': 'a_actions_000001', 'type': 'action_group', 'exports': {}}
    source['workflow']['nodes'].append(group)
    source['workflow']['nodes'][3]['parent'] = group['binding_id']
    assert any('动作组边界' in error for error in validate_canvas_graph(source['workflow']))


def test_ai_draft_accepts_native_graph_and_rejects_old_condition_graph():
    from notmyfault.host.ai_rule_draft import parse_rule_draft
    from notmyfault.host.ai_tools import ToolCallError
    source = example()
    source.pop('rule_id')
    catalog = {'triggers': {'event_aaaaaa': {'params': []}, 'event_bbbbbb': {'params': []}}, 'actions': {'record': {'params': [{'name': 'message'}]}}}
    assert parse_rule_draft(source, catalog) == source
    source['workflow']['nodes'][2]['branches'][0]['condition_graph'] = {}
    with pytest.raises(ToolCallError):
        parse_rule_draft(source, catalog)


def test_manual_test_service_uses_compiled_execution_graph():
    from notmyfault.host.api.services.rule_runs import RuleRunService
    source = example()
    captured = []
    engine = SimpleNamespace(actions_meta={'record': {'outputs': []}}, triggers_meta={'event_aaaaaa': {'outputs': []}, 'event_bbbbbb': {'outputs': []}}, run_manual_rule_snapshot=lambda *args, **kwargs: (captured.append((args, kwargs)) or True, '已运行', 'run_example'))
    service = RuleRunService(SimpleNamespace(current_engine=engine), SimpleNamespace(load_verified_rules=lambda: [source]))
    result = service.run(0, {'trigger_payloads': {'t_source_bbbbbb': {}}, 'test_assertions': [{'step_id': 'a_action_bbbbbb', 'path': [], 'operator': 'exists'}]})
    assert result['ok'] is True
    assert captured[0][0][0] == source
    assert captured[0][1]['test_assertions'][0]['step_id'] == 'a_action_bbbbbb'


def test_wait_group_uses_explicit_trigger_connections_and_timeout():
    source = example()
    choice = source['workflow']['nodes'][2]
    starter = trigger('cccccc')
    choice.update(mode='wait', timeout_seconds=2)
    source['workflow']['nodes'].append(starter)
    source['workflow']['edges'].append({'source': starter['binding_id'], 'port': 'event', 'target': choice['binding_id'], 'channel': 'control'})
    program = build_matcher(source, roots=(choice['binding_id'],))
    assert program.graph.nodes[choice['binding_id']]['mode'] == 'wait'
    assert program.graph.nodes[choice['binding_id']]['timeout_seconds'] == 2
    assert {program.graph.nodes[identity]['trigger']['binding_id'] for ids in program.leaves.values() for identity in ids} == {'t_source_aaaaaa', 't_source_bbbbbb'}
    assert 'activation' not in program.graph.nodes[choice['binding_id']]


def test_wait_group_without_trigger_is_rejected():
    source = example()
    choice = source['workflow']['nodes'][2]
    choice['mode'] = 'wait'
    source['workflow']['nodes'][0].update(type='condition', predicate={'op': 'is_true', 'left': True})
    source['workflow']['edges'][0]['port'] = 'result'
    source['workflow']['edges'] = [edge for edge in source['workflow']['edges'] if edge['source'] != 'a_source_bbbbbb' and edge['port'] != 'b_second01']
    starter = trigger('cccccc')
    source['workflow']['nodes'].append(starter)
    source['workflow']['edges'].append({'source': starter['binding_id'], 'port': 'event', 'target': choice['binding_id'], 'channel': 'control'})
    assert any('必须连接触发器' in error for error in validate_canvas_graph(source['workflow']))


@pytest.mark.parametrize('timeout', [True, float('inf'), 0, -1, 86401])
def test_wait_group_timeout_requires_finite_positive_seconds(timeout):
    source = example()
    source['workflow']['nodes'][2].update(mode='wait', timeout_seconds=timeout)
    assert any('timeout_seconds' in error for error in validate_canvas_graph(source['workflow']))


def test_connected_action_outlet_requires_a_condition_on_used_branch():
    source = example()
    source['workflow']['edges'] = [edge for edge in source['workflow']['edges'] if edge['source'] != 'a_source_bbbbbb']
    assert any('先连接该分支的条件' in error for error in validate_canvas_graph(source['workflow']))
