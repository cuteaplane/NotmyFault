from copy import deepcopy


def trigger_rule(identity, name, plugin, params=None, *, trigger_id=None):
    source = {
        'binding_id': 'a_entry_' + identity[2:], 'type': 'trigger',
        'trigger': {'binding_id': trigger_id or 't_entry_' + identity[2:], 'type': plugin, 'params': deepcopy(params or {})},
    }
    end = {'binding_id': 'a_end_' + identity[2:], 'type': 'end'}
    return {'rule_id': identity, 'name': name, 'workflow': {
        'entry': None, 'nodes': [source, end],
        'edges': [{'source': source['binding_id'], 'port': 'event', 'target': end['binding_id'], 'channel': 'control'}],
    }}


def set_rule_steps(rule, steps):
    graph = rule['workflow']
    source = graph['nodes'][0]
    graph['nodes'] = [source]
    graph['edges'] = []
    target = append_canvas_steps(graph, steps)
    if target is None:
        target = 'a_end_' + rule['rule_id'][2:]
        graph['nodes'].append({'binding_id': target, 'type': 'end'})
    graph['edges'].append({'source': source['binding_id'], 'port': 'event', 'target': target, 'channel': 'control'})
    return rule


def event_rule(identity, name, signal, steps):
    graph = {'entry': None, 'nodes': [], 'edges': []}
    def add_signal(spec, path):
        identity = 'a_signal_' + path
        if 'op' not in spec:
            leaf = deepcopy(spec)
            leaf.setdefault('binding_id', 't_signal_' + path)
            graph['nodes'].append({'binding_id': identity, 'type': 'trigger', 'trigger': leaf})
            return identity, 'event'
        op = spec['op']
        if op == 'not':
            leaf = deepcopy(spec['children'][0])
            leaf.setdefault('binding_id', 't_signal_' + path)
            graph['nodes'].append({'binding_id': identity, 'type': 'trigger', 'trigger': leaf,
                                   'occurrence': 'absent', 'duration_seconds': spec['within_seconds'],
                                   'repeat': spec.get('repeat', 'once_until_seen')})
            return identity, 'event'
        group = {'binding_id': identity, 'type': 'logic', 'op': op}
        group.update({key: deepcopy(spec[key]) for key in ('within_seconds', 'consume', 'pick', 'ttl_seconds') if key in spec})
        graph['nodes'].append(group)
        for index, child in enumerate(spec['children']):
            source, port = add_signal(child, path + '_' + str(index))
            graph['edges'].append({'source': source, 'port': port, 'target': identity, 'channel': 'condition'})
        return identity, 'result'
    source, port = add_signal(signal, identity[2:])
    first_step = len(graph['nodes'])
    target = append_canvas_steps(graph, steps)
    graph['nodes'][first_step:] = reversed(graph['nodes'][first_step:])
    if target is None:
        target = 'a_end_' + identity[2:]
        graph['nodes'].append({'binding_id': target, 'type': 'end'})
    graph['edges'].append({'source': source, 'port': port, 'target': target, 'channel': 'control'})
    return {'rule_id': identity, 'name': name, 'workflow': graph}


def native_rule(signal, steps, **fields):
    from uuid import uuid4
    identity = fields.pop('rule_id', 'r_fixture_' + uuid4().hex[:12])
    name = fields.pop('name', '测试规则')
    rule = event_rule(identity, name, signal, steps)
    rule.update(deepcopy(fields))
    return rule


def runtime_sequence(steps, **fields):
    return native_rule({'binding_id': 't_test00001', 'type': '__test_signal__', 'params': {}}, steps, **fields)


def execute_runtime(engine, key, rule, name, context):
    import threading
    from uuid import uuid4

    from notmyfault.core.canvas_workflow import CanvasProgram
    from notmyfault.core.run_manager import RunOutcome

    context['_plugins'] = engine._plugins.snapshot([])
    program = CanvasProgram(rule)
    roots = program.roots()
    if roots:
        context['_entry_root'] = roots[0]
        if program.nodes[roots[0]]['type'] == 'trigger':
            context['_entry_choices'] = {roots[0]: 'event'}
    run_id = context.setdefault('run', {}).setdefault('id', f'run_{uuid4().hex}')
    terminal = threading.Event()
    completed = {}
    terminal_types = {
        'run.succeeded', 'run.recovered', 'run.failed', 'run.cancelled',
        'run.timed_out', 'run.dropped',
    }

    def on_event(event):
        if event.run_id == run_id and event.type in terminal_types:
            completed['event'] = event
            terminal.set()

    subscription = engine.events.subscribe(on_event)
    try:
        decision = engine._run_manager.submit(rule.get('rule_id', key), rule, name, context)
        if not terminal.wait(10):
            raise TimeoutError(f'运行 {run_id} 未在限定时间内结束（提交状态：{decision}）')
        event = completed['event']
        if event.type == 'run.dropped':
            return None
        return RunOutcome(event.type.removeprefix('run.'), event.data, event.error)
    finally:
        engine.events.unsubscribe(subscription)


def branch_rule(signal, branches, *, following=(), group_id='a_branch01', **fields):
    rule = native_rule(signal, [], **fields)
    graph = rule['workflow']
    graph['nodes'].pop()
    inlet = graph['edges'].pop()
    group = {'binding_id': group_id, 'type': 'event_group', 'group_type': 'if', 'branches': []}
    graph['nodes'].append(group)
    inlet['target'] = group_id
    graph['edges'].append(inlet)
    index = 0
    def operand(predicate):
        nonlocal index
        identity = f'a_predicate_{index:06d}'
        index += 1
        op = predicate.get('op')
        if op == 'event_occurred':
            source = next((node for node in graph['nodes'] if node.get('type') == 'trigger' and node['trigger']['binding_id'] == predicate['trigger_id']), None)
            if source is None:
                source = {'binding_id': identity, 'type': 'trigger', 'trigger': {'binding_id': predicate['trigger_id'], 'type': '__unknown__', 'params': {}}}
                graph['nodes'].append(source)
            return source['binding_id'], 'event'
        node = {'binding_id': identity, 'parent': group_id}
        if op in ('all', 'any', 'not'):
            node.update(type='logic', op=op)
            graph['nodes'].append(node)
            for child in predicate['children']:
                source, port = operand(child)
                graph['edges'].append({'source': source, 'port': port, 'target': identity, 'channel': 'condition'})
        else:
            node.update(type='condition', predicate=deepcopy(predicate))
            graph['nodes'].append(node)
        return identity, 'result'
    exits = []
    for number, branch in enumerate(branches):
        port = f'b_branch_{number:06d}'
        record = {'id': port}
        predicate = branch.get('predicate')
        if predicate is None:
            record['else'] = True
        else:
            source, output = operand(predicate)
            graph['edges'].append({'source': source, 'port': output, 'target': group_id, 'target_port': port, 'channel': 'condition'})
        group['branches'].append(record)
        exits.append((port, branch.get('steps', [])))
    target = append_canvas_steps(graph, following)
    for port, steps in exits:
        first = append_canvas_steps(graph, steps, successor=target)
        if first is not None:
            graph['edges'].append({'source': group_id, 'port': port, 'target': first, 'channel': 'control'})
    return rule


def execute_sequence(engine, steps, name, context):
    context['_variables_initialized'] = True
    return execute_runtime(engine, 'r_executor001', runtime_sequence(steps), name, context).status == 'succeeded'


def selected_branch(label):
    return 'b_then0001' if label == 'then' else 'b_else0001' if label == 'else' else 'b_elif' + f'{int(label[5:-1]):06d}'


def append_canvas_steps(graph, steps, *, successor=None):
    index = len(graph['nodes'])
    def add_predicate(predicate, parent):
        nonlocal index
        index += 1
        identity = f'a_condition_{index:06d}'
        op = predicate.get('op')
        if op == 'event_occurred':
            source = next((node for node in graph['nodes'] if node.get('type') == 'trigger' and node['trigger']['binding_id'] == predicate['trigger_id']), None)
            if source is None:
                source = {'binding_id': identity + '_event', 'type': 'trigger', 'parent': parent,
                          'trigger': {'binding_id': predicate['trigger_id'], 'type': '__test_event__', 'params': {}}}
                graph['nodes'].append(source)
            if predicate.get('expected', True) == (source.get('occurrence', 'occurred') != 'absent'):
                return source['binding_id'], 'event'
            node = {'binding_id': identity, 'type': 'logic', 'op': 'not', 'parent': parent}
            graph['nodes'].append(node)
            graph['edges'].append({'source': source['binding_id'], 'port': 'event', 'target': identity, 'channel': 'condition'})
        elif op in ('all', 'any', 'not'):
            graph['nodes'].append({'binding_id': identity, 'type': 'logic', 'op': op, 'parent': parent})
            for child in predicate['children']:
                source, port = add_predicate(child, parent)
                graph['edges'].append({'source': source, 'port': port, 'target': identity, 'channel': 'condition'})
        else:
            graph['nodes'].append({'binding_id': identity, 'type': 'condition', 'predicate': deepcopy(predicate), 'parent': parent})
        return identity, 'result'
    def sequence(items, successor=None, parent=None):
        nonlocal index
        current = successor
        for item in reversed(deepcopy(items)):
            index += 1
            identity = item.get('binding_id', f'a_action_{index:06d}')
            if item.get('type') == '__decision_fixture__':
                group = {'binding_id': identity, 'type': 'event_group', 'group_type': 'if', 'branches': []}
                if parent:
                    group['parent'] = parent
                graph['nodes'].append(group)
                branches = [{'predicate': item['predicate'], 'then_steps': item.get('then_steps', [])},
                            *item.get('alternatives', []), {'else': True, 'then_steps': item.get('else_steps', [])}]
                for order, branch in enumerate(branches):
                    port = 'b_else0001' if branch.get('else') else 'b_then0001' if order == 0 else f'b_elif{order - 1:06d}'
                    record = {'id': port}
                    if branch.get('else'):
                        record['else'] = True
                    else:
                        source, output = add_predicate(branch['predicate'], identity)
                        graph['edges'].append({'source': source, 'port': output, 'target': identity, 'target_port': port, 'channel': 'condition'})
                    group['branches'].append(record)
                    target = sequence(branch['then_steps'], current, parent)
                    if target is not None:
                        graph['edges'].append({'source': identity, 'port': port, 'target': target, 'channel': 'control'})
            else:
                item['binding_id'] = identity
                if parent:
                    item['parent'] = parent
                failures = item.pop('failure_actions', [])
                graph['nodes'].append(item)
                if failures:
                    handler_id = 'a_recovery_' + identity[2:]
                    handler = {'binding_id': handler_id, 'type': 'action_group'}
                    if parent:
                        handler['parent'] = parent
                    graph['nodes'].append(handler)
                    for order, failure in enumerate(failures):
                        failure.setdefault('binding_id', identity + f'_failure{order}')
                    first = sequence(failures, parent=handler_id)
                    graph['edges'].append({'source': identity, 'port': 'failure', 'target': handler_id, 'channel': 'control'})
                    graph['edges'].append({'source': handler_id, 'port': 'entry', 'target': first, 'channel': 'control'})
                if current is not None:
                    graph['edges'].append({'source': identity, 'port': 'next', 'target': current, 'channel': 'control'})
            current = identity
        return current
    return sequence(steps, successor)
