from __future__ import annotations

import copy
import hashlib
import math
import re
import secrets
from collections import defaultdict


_BRANCH_ID = re.compile(r'^b_[a-z0-9_]{1,64}$')

NATIVE_NODES = frozenset({'if', 'set_variable', 'event_group', 'action_group', 'merge', 'end'})


def _identity(prefix, seed=None):
    token = secrets.token_hex(6) if seed is None else hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]
    return f'{prefix}_{token}'


def iter_graph_nodes(graph, path='workflow'):
    if not isinstance(graph, dict):
        return
    nodes = graph.get('nodes', [])
    if not isinstance(nodes, list):
        return
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        location = f'{path}.nodes[{index}]'
        yield node, location
        if node.get('type') == 'action_group':
            yield from iter_graph_nodes(node.get('workflow'), f'{location}.workflow')
        from notmyfault.core.rules import iter_action_nodes
        yield from iter_action_nodes(node.get('failure_actions'), f'{location}.failure_actions')


def iter_rule_actions(rule, *, include_invalid=False):
    if isinstance(rule.get('workflow'), dict):
        yield from iter_graph_nodes(rule['workflow'])
    else:
        from notmyfault.core.rules import iter_action_nodes
        yield from iter_action_nodes(rule.get('actions'), include_invalid=include_invalid)


def iter_rule_event_nodes(rule):
    from notmyfault.core.conditions import get_rule_events, iter_condition_events
    yield from get_rule_events(rule)
    for node, _path in iter_rule_actions(rule):
        if node.get('type') == 'event_group':
            yield from iter_condition_events(node.get('activation'))
            triggers = node.get('triggers', [])
            for trigger in triggers if isinstance(triggers, list) else []:
                if isinstance(trigger, dict):
                    yield trigger


def get_all_rule_events(rule):
    result, seen = [], set()
    for leaf in iter_rule_event_nodes(rule):
        identity = leaf.get('binding_id')
        if not isinstance(identity, str):
            result.append(leaf)
        elif identity not in seen:
            seen.add(identity)
            result.append(leaf)
    return result


def condition_graph_predicate(graph, *, node_ids=None):
    if not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list) or not isinstance(graph.get('edges'), list):
        raise ValueError('condition_graph 必须包含 nodes 和 edges 数组')
    nodes, incoming = {}, defaultdict(list)
    for node in graph['nodes']:
        if not isinstance(node, dict) or not isinstance(node.get('id'), str) or not node['id']:
            raise ValueError('条件节点必须提供 id')
        if node['id'] in nodes:
            raise ValueError(f'条件节点身份重复: {node["id"]}')
        nodes[node['id']] = node
    pairs = set()
    for edge in graph['edges']:
        if not isinstance(edge, dict) or not isinstance(edge.get('source'), str) or not isinstance(edge.get('target'), str) or edge['source'] not in nodes or edge['target'] not in nodes:
            raise ValueError('条件连线引用的节点不存在')
        order = edge.get('order', 0)
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise ValueError('条件连线 order 必须是非负整数')
        pair = edge['source'], edge['target']
        if pair in pairs:
            raise ValueError('条件连线重复')
        pairs.add(pair)
        incoming[edge['target']].append((order, edge['source']))
    visiting, used = set(), set()

    def visit(identity):
        if not isinstance(identity, str) or identity not in nodes:
            raise ValueError('条件图 root 引用的节点不存在')
        if identity in visiting:
            raise ValueError('条件图不能包含环')
        visiting.add(identity)
        used.add(identity)
        node, children = nodes[identity], incoming[identity]
        if 'predicate' in node:
            if children or 'op' in node:
                raise ValueError('比较条件不能接收条件连线或同时设置 op')
            result = copy.deepcopy(node['predicate'])
        else:
            op = node.get('op')
            if op not in ('all', 'any', 'not') or not children or op == 'not' and len(children) != 1:
                raise ValueError('条件组需要有效的 all、any 或单输入 not')
            if len({order for order, _child in children}) != len(children):
                raise ValueError('同一条件组的输入顺序不能重复')
            result = {'op': op, 'children': [visit(child) for _order, child in sorted(children)]}
        visiting.remove(identity)
        if node_ids is not None:
            node_ids[id(result)] = identity
        return result

    result = visit(graph.get('root'))
    if used != set(nodes):
        raise ValueError('条件图包含未连接到 root 的节点')
    from notmyfault.core.predicates import validate_predicate
    errors = validate_predicate(result)
    if errors:
        raise ValueError('；'.join(errors))
    return result


def predicate_condition_graph(predicate, *, seed=None):
    nodes, edges = [], []

    def visit(current, path='root'):
        identity = _identity('p', f'{seed}:{path}' if seed is not None else None)
        if current.get('op') in ('all', 'any', 'not'):
            nodes.append({'id': identity, 'op': current['op']})
            for order, child in enumerate(current.get('children', [])):
                edges.append({'source': visit(child, f'{path}.{order}'), 'target': identity, 'order': order})
        else:
            nodes.append({'id': identity, 'predicate': copy.deepcopy(current)})
        return identity

    root = visit(predicate)
    return {'root': root, 'nodes': nodes, 'edges': edges}


def branch_predicate(branch):
    return condition_graph_predicate(branch['condition_graph']) if 'condition_graph' in branch else branch.get('condition')


def evaluate_group_branches(node, context, evaluated, *, status_lookup, occurred):
    from notmyfault.core.predicates import evaluate_predicate
    for branch in node['branches']:
        if branch.get('else'):
            return branch['id']
        identities, predicates = {}, []
        predicate = condition_graph_predicate(branch['condition_graph'], node_ids=identities) if 'condition_graph' in branch else branch.get('condition')
        record = {'branch_id': branch['id'], 'predicates': predicates}
        evaluated.append(record)
        def on_result(current, path, result, error):
            item = {'path': path, 'op': current.get('op')}
            if id(current) in identities:
                item['node_id'] = identities[id(current)]
            item.update({'result': result} if error is None else {'error': {'code': getattr(error, 'code', 'condition_evaluation_failed')}})
            predicates.append(item)
        try:
            result = evaluate_predicate(predicate, context, status_lookup=status_lookup, _event_occurrences=occurred, _on_result=on_result)
        except (ValueError, TypeError) as error:
            record['error'] = {'code': getattr(error, 'code', 'condition_evaluation_failed')}
            raise
        record['result'] = result
        if result:
            return branch['id']
    raise ValueError('事件组没有可执行出口')


def graph_index(graph):
    return {node['binding_id']: node for node in graph.get('nodes', [])}


def graph_successors(graph):
    return {(edge['source'], edge.get('port', 'next')): edge.get('target') for edge in graph.get('edges', [])}


def find_graph_node(graph, identity):
    for node in graph.get('nodes', []):
        if node.get('binding_id') == identity:
            return graph, node
        if node.get('type') == 'action_group':
            found = find_graph_node(node['workflow'], identity)
            if found:
                return found
    return None


def find_graph_scope(graph, identity, inherited=(), group_path=(), inherited_possible=None):
    available = graph_available_steps(graph, inherited)
    possible = graph_possible_steps(graph, inherited if inherited_possible is None else inherited_possible)
    for node in graph.get('nodes', []):
        node_id = node.get('binding_id')
        if node_id == identity:
            return {'graph': graph, 'node': node, 'available': frozenset(available[node_id]),
                    'possible': frozenset(possible[node_id]), 'group_path': tuple(group_path)}
        if node.get('type') == 'action_group':
            found = find_graph_scope(node['workflow'], identity, available[node_id], (*group_path, node_id), possible[node_id])
            if found is not None:
                return found
    return None


def entry_event_group(graph):
    current = graph
    while isinstance(current, dict):
        nodes = current.get('nodes', [])
        if not isinstance(nodes, list):
            return None
        node = next((item for item in nodes if isinstance(item, dict) and item.get('binding_id') == current.get('entry')), None)
        if node is None:
            return None
        if node.get('type') == 'event_group':
            return node if isinstance(node.get('activation'), dict) else None
        if node.get('type') != 'action_group':
            return None
        current = node.get('workflow')
    return None


def normalize_workflow_rule(rule):
    result = copy.deepcopy(rule)
    if isinstance(result.get('workflow'), dict):
        for node, _path in iter_rule_actions(result):
            if node.get('type') == 'event_group':
                for branch in node.get('branches', []):
                    if 'condition' in branch and 'condition_graph' not in branch:
                        branch['condition_graph'] = predicate_condition_graph(branch.pop('condition'), seed=node['binding_id'] + ':' + branch['id'])
        return result
    from notmyfault.core.rule_model import ensure_rule_binding_ids
    result = ensure_rule_binding_ids(result)
    graph = {'entry': None, 'nodes': [], 'edges': []}

    def sequence(items, continuation):
        target = continuation
        for original in reversed(items):
            node = copy.deepcopy(original)
            identity = node['binding_id']
            if node.get('type') == 'if':
                branches = []
                alternatives = [('IF', node['condition'], node.get('then', []))]
                alternatives.extend(('ELIF', branch['condition'], branch.get('then', [])) for branch in node.get('elif', []))
                alternatives.append(('ELSE', None, node.get('else', [])))
                for branch_index, (label, predicate, actions) in enumerate(alternatives):
                    branch = {'id': _identity('b', identity + ':' + str(branch_index)), 'label': label}
                    if predicate is None:
                        branch['else'] = True
                    else:
                        branch['condition_graph'] = predicate_condition_graph(predicate, seed=identity + ':' + branch['id'])
                    branches.append(branch)
                    graph['edges'].append({'source': identity, 'port': branch['id'], 'target': sequence(actions, target)})
                node = {key: value for key, value in node.items() if key not in ('condition', 'then', 'elif', 'else')}
                node.update(type='event_group', mode='immediate', branches=branches)
            else:
                graph['edges'].append({'source': identity, 'port': 'next', 'target': target})
            graph['nodes'].append(node)
            target = identity
        return target

    target = sequence(result.pop('actions', []), None)
    if 'condition' in result:
        branch_id, identity = _identity('b', result['rule_id'] + ':entry'), _identity('a', result['rule_id'] + ':entry')
        graph['nodes'].append({'type': 'event_group', 'binding_id': identity, 'activation': result.pop('condition'),
                               'mode': 'immediate', 'branches': [{'id': branch_id, 'else': True, 'label': '执行'}]})
        graph['edges'].append({'source': identity, 'port': branch_id, 'target': target})
        target = identity
    graph['entry'] = target
    result['workflow'] = graph
    return result


def validate_workflow_graph(graph, path='workflow', seen=None):
    from notmyfault.core.rule_model import _BINDING_ID_RE
    from notmyfault.core.conditions import validate_condition_tree
    seen = set() if seen is None else seen
    errors = []
    if not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list) or not isinstance(graph.get('edges'), list):
        return [f'{path} 必须包含 nodes 和 edges 数组']
    nodes, ports = {}, {}
    for index, node in enumerate(graph['nodes']):
        location = f'{path}.nodes[{index}]'
        if not isinstance(node, dict):
            errors.append(f'{location} 必须是对象')
            continue
        identity = node.get('binding_id')
        if not isinstance(identity, str) or not _BINDING_ID_RE.fullmatch(identity) or not identity.startswith('a_'):
            errors.append(f'{location}.binding_id 无效')
            continue
        if identity in seen:
            errors.append(f'{location}.binding_id 与其他节点重复')
        seen.add(identity)
        nodes[identity] = node
        ports[identity] = {'next'}
        if not isinstance(node.get('type'), str) or not node['type']:
            errors.append(f'{location}.type 不能为空')
        if 'name' in node and not isinstance(node['name'], str):
            errors.append(f'{location}.name 必须是文本')
        if node.get('type') in ('action_group', 'event_group'):
            for field in ('params', 'retry', 'retry_delay_seconds', 'retry_backoff', 'failure_actions'):
                if field in node:
                    errors.append(f'{location} 的流程组不支持 {field}')
            if node.get('on_error', 'stop') not in ('stop', 'continue'):
                errors.append(f'{location}.on_error 必须是 stop 或 continue')
            if node.get('type') == 'event_group' and node.get('on_error', 'stop') != 'stop':
                errors.append(f'{location} 的事件组失败后必须停止，不支持 on_error:continue')
            if 'timeout_seconds' in node and (node.get('type') == 'action_group' or node.get('mode', 'immediate') != 'wait'):
                errors.append(f'{location} 只有等待事件组支持 timeout_seconds')
        if node.get('type') == 'if':
            errors.append(f'{location} 请使用 event_group')
        if node.get('type') == 'action_group':
            errors.extend(validate_workflow_graph(node.get('workflow'), f'{location}.workflow', seen))
            if not isinstance(node.get('exports', {}), dict):
                errors.append(f'{location}.exports 必须是对象')
        if node.get('type') == 'event_group':
            branches = node.get('branches')
            ports[identity] = set()
            if not isinstance(branches, list) or not branches:
                errors.append(f'{location}.branches 至少需要一个分支')
                continue
            for branch_index, branch in enumerate(branches):
                branch_path = f'{location}.branches[{branch_index}]'
                if not isinstance(branch, dict) or not isinstance(branch.get('id'), str) or not _BRANCH_ID.fullmatch(branch['id']):
                    errors.append(f'{branch_path}.id 必须以 b_ 开头，后接小写字母、数字或下划线')
                    continue
                if branch['id'] in ports[identity]:
                    errors.append(f'{branch_path}.id 重复')
                ports[identity].add(branch['id'])
                if 'condition' in branch and 'condition_graph' in branch:
                    errors.append(f'{branch_path} 不能同时保存 condition 和 condition_graph')
                if branch.get('else') is True:
                    if branch_index != len(branches) - 1 or 'condition_graph' in branch or 'condition' in branch:
                        errors.append(f'{branch_path} ELSE 必须是最后一个分支且不能有条件')
                else:
                    try:
                        predicate = branch_predicate(branch)
                        from notmyfault.core.predicates import validate_predicate
                        errors.extend(f'{branch_path}: {error}' for error in validate_predicate(predicate))
                    except (ValueError, TypeError, KeyError) as error:
                        errors.append(f'{branch_path}: {error}')
            if not isinstance(branches[-1], dict) or branches[-1].get('else') is not True:
                errors.append(f'{location}.branches 必须提供 ELSE 出口')
            if node.get('mode', 'immediate') not in ('immediate', 'wait'):
                errors.append(f'{location}.mode 必须是 immediate 或 wait')
            if node.get('mode') == 'wait' and not isinstance(node.get('activation'), dict):
                errors.append(f'{location} 等待事件组必须提供 activation')
            if 'activation' in node:
                errors.extend(f'{location}.activation: {error}' for error in validate_condition_tree(node['activation']))
            if 'timeout_seconds' in node:
                value = node['timeout_seconds']
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 86400:
                    errors.append(f'{location}.timeout_seconds 必须是 0 到 86400 之间的秒数')
            if not isinstance(node.get('triggers', []), list):
                errors.append(f'{location}.triggers 必须是数组')
            else:
                for trigger_index, trigger in enumerate(node.get('triggers', [])):
                    if not isinstance(trigger, dict) or not isinstance(trigger.get('type'), str) or 'children' in trigger:
                        errors.append(f'{location}.triggers[{trigger_index}] 必须是触发器节点')
                    else:
                        errors.extend(f'{location}.triggers[{trigger_index}]: {error}' for error in validate_condition_tree(trigger))
        if node.get('type') == 'end':
            ports[identity] = set()
    entry = graph.get('entry')
    if entry is not None and (not isinstance(entry, str) or entry not in nodes) or entry is None and nodes:
        errors.append(f'{path}.entry 必须引用本图节点')
    outgoing, adjacency = set(), defaultdict(list)
    for index, edge in enumerate(graph['edges']):
        location = f'{path}.edges[{index}]'
        if not isinstance(edge, dict) or not isinstance(edge.get('source'), str) or edge['source'] not in nodes or edge.get('target') is not None and (not isinstance(edge['target'], str) or edge['target'] not in nodes):
            errors.append(f'{location} 只能连接同一图中的节点')
            continue
        source, port = edge['source'], edge.get('port', 'next')
        if not isinstance(port, str):
            errors.append(f'{location}.port 必须是文本')
            continue
        if port not in ports[source]:
            errors.append(f'{location}.port 不存在')
        if (source, port) in outgoing:
            errors.append(f'{location} 同一控制出口只能连接一个目标')
        outgoing.add((source, port))
        if edge.get('target') is not None:
            adjacency[source].append(edge['target'])
    for identity, available_ports in ports.items():
        for port in available_ports:
            if (identity, port) not in outgoing:
                errors.append(f'{path} 节点 {identity} 的出口 {port} 需要连接节点或显式结束')
    visiting, visited = set(), set()

    def visit(identity):
        if identity in visiting:
            errors.append(f'{path} 控制流不能包含环')
            return
        if identity in visited:
            return
        visiting.add(identity)
        for target in adjacency[identity]:
            visit(target)
        visiting.remove(identity)
        visited.add(identity)
    for identity in nodes:
        visit(identity)
    reachable = set()
    pending = [entry] if isinstance(entry, str) and entry in nodes else []
    while pending:
        identity = pending.pop()
        if identity not in reachable:
            reachable.add(identity)
            pending.extend(adjacency[identity])
    if set(nodes) - reachable:
        errors.append(f'{path} 包含无法从入口到达的节点')
    return errors


def graph_available_steps(graph, inherited=()):
    nodes, successors = graph_index(graph), graph_successors(graph)
    incoming = defaultdict(list)
    for (source, _port), target in successors.items():
        if target is not None:
            incoming[target].append(source)
    memo = {}

    def before(identity):
        if identity in memo:
            return memo[identity]
        predecessors = incoming[identity]
        paths = [before(source) | {source} for source in predecessors]
        value = set(inherited) if identity == graph.get('entry') or not paths else set.intersection(*paths)
        memo[identity] = value
        return value

    for identity in nodes:
        before(identity)
    return memo


def graph_possible_steps(graph, inherited=()):
    incoming = defaultdict(list)
    for edge in graph.get('edges', []):
        if edge.get('target') is not None:
            incoming[edge['target']].append(edge['source'])
    memo = {}
    def before(identity):
        if identity not in memo:
            memo[identity] = set(inherited).union(*(before(source) | {source} for source in incoming[identity]))
        return memo[identity]
    for node in graph.get('nodes', []):
        before(node['binding_id'])
    return memo
