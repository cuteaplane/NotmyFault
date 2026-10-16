from __future__ import annotations

from notmyfault.core.canvas_workflow import CanvasProgram


NATIVE_NODES = frozenset({'trigger', 'condition', 'logic', 'set_variable', 'event_group', 'action_group', 'merge', 'end'})
NON_ACTION_NODES = NATIVE_NODES - {'set_variable'}


def iter_graph_nodes(graph, path='workflow'):
    if not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list):
        return
    for index, node in enumerate(graph['nodes']):
        if isinstance(node, dict):
            yield node, f'{path}.nodes[{index}]'


def iter_rule_actions(rule):
    yield from iter_graph_nodes(rule.get('workflow'))


def iter_rule_event_nodes(rule):
    for node, _path in iter_rule_actions(rule):
        if node.get('type') == 'trigger' and isinstance(node.get('trigger'), dict):
            yield node['trigger']


def get_all_rule_events(rule):
    return list(iter_rule_event_nodes(rule))


def graph_index(graph):
    return {node['binding_id']: node for node in graph.get('nodes', [])}


def graph_successors(graph):
    return {(edge['source'], edge['port']): edge['target'] for edge in graph.get('edges', [])
            if edge.get('channel') == 'control'}


def execution_nodes(graph, start_id=None, end_id=None):
    program = CanvasProgram({'workflow': graph})
    entries = (start_id,) if start_id else program.roots()
    identities = frozenset(identity for entry in entries for identity in program.reachable(entry, end_id))
    for node, path in iter_graph_nodes(graph):
        if node['binding_id'] in identities:
            yield node, path


def find_graph_node(graph, identity):
    node = graph_index(graph).get(identity)
    return (graph, node) if node is not None else None


def _steps_before(graph, inherited=(), *, possible=False):
    program = CanvasProgram({'workflow': graph})
    memo, visiting = {}, set()
    consumers = {}
    for edge in program.edges:
        if edge['channel'] == 'condition':
            consumers.setdefault(edge['source'], set()).add(edge['target'])
    handlers = {target for (source, port), target in program.successors.items() if port == 'failure'}

    def evaluation_points(identity):
        points, pending, seen = set(), list(consumers.get(identity, ())), {identity}
        while pending:
            target = pending.pop()
            if target in seen:
                continue
            seen.add(target)
            if program.predecessors.get(target) or target in program.control_sources:
                points.add(target)
            else:
                pending.extend(consumers.get(target, ()))
        return points

    def before(identity):
        if identity in memo:
            return memo[identity]
        if identity in visiting:
            raise ValueError('工作流控制关系不能包含环')
        visiting.add(identity)
        incoming = program.predecessors.get(identity, ())
        paths = []
        for edge in incoming:
            source = edge['source']
            completed = set(before(source))
            recovery = bool(handlers.intersection(program.paths[source]))
            if edge['port'] not in ('entry', 'failure') and (possible or recovery or program.nodes[source].get('on_error') != 'continue'):
                completed.add(source)
            if possible and edge['port'] == 'next' and program.nodes[source].get('on_error') == 'continue':
                handler = program.successors.get((source, 'failure'))
                if handler is not None:
                    completed.add(handler)
            paths.append(completed)
        if paths:
            value = set.union(*paths) if possible else set.intersection(*paths)
        else:
            points = evaluation_points(identity) if identity not in program.control_sources else ()
            points = [before(point) for point in points]
            if points:
                value = set.union(*points) if possible else set.intersection(*points)
            else:
                parent = program.nodes[identity].get('parent')
                value = set(before(parent)) if parent is not None else set(inherited)
        memo[identity] = value
        visiting.remove(identity)
        return value

    for identity in program.nodes:
        before(identity)
    return memo


def graph_available_steps(graph, inherited=()):
    return _steps_before(graph, inherited)


def graph_possible_steps(graph, inherited=()):
    return _steps_before(graph, inherited, possible=True)


def group_exit_steps(graph, identity, inherited=(), *, possible=False):
    program = CanvasProgram({'workflow': graph})
    before = _steps_before(graph, inherited, possible=possible)
    entry = program.successors.get((identity, 'entry'))
    reachable = program.reachable(entry) if entry is not None else ()
    handlers = {target for (source, port), target in program.successors.items() if port == 'failure'}
    scoped = {node_id for node_id in reachable if program.scopes[node_id] == identity and node_id not in handlers}
    exits = [node_id for node_id in scoped if program.nodes[node_id]['type'] == 'end' or
        (program.nodes[node_id]['type'] not in ('trigger', 'condition', 'logic') and
         not any(source == node_id and port not in ('failure', 'entry') for source, port in program.successors))]
    paths = [before[node_id] | {node_id} for node_id in exits]
    for node_id in scoped:
        node = program.nodes[node_id]
        if node.get('group_type') == 'if':
            for branch in node['branches']:
                if (branch.get('else') or program.operands(node_id, branch['id'])) and (node_id, branch['id']) not in program.successors:
                    paths.append(before[node_id] | {node_id})
        elif node['type'] in ('logic', 'event_group'):
            paths.append(before[node_id] | {node_id})
    return (set.union(*paths) if possible else set.intersection(*paths)) if paths else set(before[identity])


def find_graph_scope(graph, identity, inherited=(), group_path=(), inherited_possible=None):
    program = CanvasProgram({'workflow': graph})
    node = program.nodes.get(identity)
    if node is None:
        return None
    available = graph_available_steps(graph, inherited)
    possible = graph_possible_steps(graph, inherited if inherited_possible is None else inherited_possible)
    return {'graph': graph, 'node': node, 'available': frozenset(available[identity]),
            'possible': frozenset(possible[identity]), 'group_path': (*group_path, *program.paths[identity])}


def entry_event_group(graph):
    if not isinstance(graph, dict):
        return None
    program = CanvasProgram({'workflow': graph})
    roots = program.roots()
    if len(roots) != 1:
        return None
    node = program.nodes[roots[0]]
    while node['type'] == 'action_group':
        entry = program.successors.get((node['binding_id'], 'entry'))
        if entry is None:
            return None
        node = program.nodes[entry]
    return node if node['type'] in ('event_group', 'logic', 'trigger') else None


def evaluate_group_branches(node, context, evaluated, *, program, status_lookup, occurred):
    records = []
    error = None
    try:
        selected = program.evaluate(node['binding_id'], context, status_lookup=status_lookup,
            occurred=occurred, records=records)
    except (ValueError, TypeError) as caught:
        error = caught
    if node.get("group_type") == "if":
        predicates = []
        for record in records:
            if "branch_id" in record:
                evaluated.append({**record, "predicates": predicates})
                predicates = []
            else:
                predicates.append(record)
    else:
        outcome = {'error': {'code': getattr(error, 'code', 'condition_evaluation_failed')}} if error else {'result': selected is not None and selected is not False}
        evaluated.append({"node_id": node["binding_id"], "predicates": records, **outcome})
    if error is not None:
        raise error
    return selected if node.get('group_type') == 'if' else 'result' if selected else None


