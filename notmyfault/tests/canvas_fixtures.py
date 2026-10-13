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
    nodes = deepcopy(steps)
    def assign(node, suffix):
        node.setdefault('binding_id', 'a_step_' + rule['rule_id'][2:] + '_' + suffix)
        failures = node.get('failure_actions', [])
        if isinstance(failures, list):
            for index, failure in enumerate(failures):
                if isinstance(failure, dict):
                    assign(failure, suffix + '_failure' + str(index))
    for index, node in enumerate(nodes):
        assign(node, str(index))
    if not nodes:
        nodes = [{'binding_id': 'a_end_' + rule['rule_id'][2:], 'type': 'end'}]
    graph['nodes'] = [source, *nodes]
    graph['edges'] = [{'source': source['binding_id'], 'port': 'event', 'target': nodes[0]['binding_id'], 'channel': 'control'}]
    graph['edges'].extend({'source': left['binding_id'], 'port': 'next', 'target': right['binding_id'], 'channel': 'control'}
                          for left, right in zip(nodes, nodes[1:]))
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
    from notmyfault.core.workflow_graph import predicate_condition_graph
    nodes, edges = [], []
    index = 0
    def sequence(items, successor=None):
        nonlocal index
        current = successor
        for item in reversed(deepcopy(items)):
            index += 1
            identity = item.get('binding_id', f'a_runtime_{index:06d}')
            if item.get('type') == '__decision_fixture__':
                alternatives = [{'predicate': item['predicate'], 'then_steps': item.get('then_steps', [])},
                                *item.get('alternatives', []), {'else': True, 'then_steps': item.get('else_steps', [])}]
                group = {'binding_id': identity, 'type': 'event_group', 'branches': []}
                for branch_index, branch in enumerate(alternatives):
                    label = 'b_else0001' if branch.get('else') else 'b_then0001' if branch_index == 0 else f'b_elif{branch_index - 1:06d}'
                    record = {'id': label}
                    if branch.get('else'):
                        record['else'] = True
                    else:
                        record['condition_graph'] = predicate_condition_graph(branch['predicate'])
                    group['branches'].append(record)
                    target = sequence(branch['then_steps'], current)
                    edges.append({'source': identity, 'port': label, 'target': target})
                nodes.append(group)
            else:
                item['binding_id'] = identity
                for failure_index, failure in enumerate(item.get('failure_actions', [])):
                    failure.setdefault('binding_id', identity + f'_failure{failure_index}')
                nodes.append(item)
                edges.append({'source': identity, 'port': 'next', 'target': current})
            current = identity
        return current
    entry = sequence(steps)
    return {**deepcopy(fields), 'workflow': {'entry': entry, 'nodes': list(reversed(nodes)), 'edges': edges}}


def execute_runtime(engine, key, rule, name, context):
    context['_plugins'] = engine._plugins.snapshot([])
    return engine._run_manager.execute_sync(key, rule, name, context)


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
        nodes = runtime_sequence(branch.get('steps', []))['workflow']['nodes']
        for step_number, node in enumerate(nodes):
            if node['binding_id'].startswith('a_runtime_'):
                node['binding_id'] = f'a_branchstep_{number:06d}_{step_number:06d}'
            node['parent'] = group_id
        graph['nodes'].extend(nodes)
        previous, output = group_id, port
        for node in nodes:
            graph['edges'].append({'source': previous, 'port': output, 'target': node['binding_id'], 'channel': 'control'})
            previous, output = node['binding_id'], 'next'
        exits.append((previous, output))
    following_nodes = runtime_sequence(following)['workflow']['nodes']
    graph['nodes'].extend(following_nodes)
    if following_nodes:
        for source, port in exits:
            graph['edges'].append({'source': source, 'port': port, 'target': following_nodes[0]['binding_id'], 'channel': 'control'})
        graph['edges'].extend({'source': left['binding_id'], 'port': 'next', 'target': right['binding_id'], 'channel': 'control'}
                              for left, right in zip(following_nodes, following_nodes[1:]))
    return rule


def execute_sequence(engine, steps, name, context):
    context['_variables_initialized'] = True
    return execute_runtime(engine, 'r_executor001', runtime_sequence(steps), name, context).status == 'succeeded'


def selected_branch(label):
    return 'b_then0001' if label == 'then' else 'b_else0001' if label == 'else' else 'b_elif' + f'{int(label[5:-1]):06d}'


def append_canvas_steps(graph, steps):
    index = 0
    def add_predicate(predicate, parent):
        nonlocal index
        index += 1
        identity = f'a_condition_{index:06d}'
        op = predicate.get('op')
        if op == 'event_occurred':
            source = next(node for node in graph['nodes'] if node.get('type') == 'trigger' and node['trigger']['binding_id'] == predicate['trigger_id'])
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
                failures = item.get('failure_actions', [])
                if isinstance(failures, list):
                    for order, failure in enumerate(failures):
                        if isinstance(failure, dict):
                            failure.setdefault('binding_id', identity + f'_failure{order}')
                graph['nodes'].append(item)
                if current is not None:
                    graph['edges'].append({'source': identity, 'port': 'next', 'target': current, 'channel': 'control'})
            current = identity
        return current
    return sequence(steps)
