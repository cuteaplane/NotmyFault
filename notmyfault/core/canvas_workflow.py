from __future__ import annotations

import math
from collections import defaultdict
from types import MappingProxyType


CANVAS_NODES = frozenset({'trigger', 'condition', 'logic', 'event_group', 'action_group'})
GROUP_TYPES = frozenset({'if', 'all', 'any', 'not'})


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def canvas_nodes(rule):
    return rule.get('workflow', {}).get('nodes', [])


def trigger_leaves(rule):
    return [node['trigger'] for node in canvas_nodes(rule) if node.get('type') == 'trigger']


def validate_canvas_graph(graph):
    from notmyfault.core.rule_model import _BINDING_ID_RE
    errors = []
    if not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list) or not isinstance(graph.get('edges'), list):
        return ['workflow 必须包含 nodes 和 edges 数组']
    if graph.get('entry') is not None:
        errors.append('平面流程图的入口由连线确定，entry 必须为 null')
    nodes, identities, branches = {}, set(), {}
    for index, node in enumerate(graph['nodes']):
        path = f'workflow.nodes[{index}]'
        if not isinstance(node, dict):
            errors.append(f'{path} 必须是对象')
            continue
        identity = node.get('binding_id')
        if not isinstance(identity, str) or not _BINDING_ID_RE.fullmatch(identity) or not identity.startswith('a_'):
            errors.append(f'{path}.binding_id 无效')
            continue
        if identity in identities:
            errors.append(f'{path}.binding_id 重复')
        identities.add(identity)
        nodes[identity] = node
        kind = node.get('type')
        if not isinstance(kind, str) or not kind:
            errors.append(f'{path}.type 不能为空')
        obsolete = set(node) & {'activation', 'condition_graph', 'workflow', 'then', 'elif', 'else', 'triggers', 'failure_actions'}
        if kind == 'if' or obsolete:
            errors.append(f'{path} 使用了已移除的条件模型: {", ".join(sorted(obsolete)) or "if"}')
        if kind in CANVAS_NODES:
            unsupported = set(node) & {'params', 'retry', 'retry_delay_seconds', 'retry_backoff'}
            if kind != 'action_group' and 'on_error' in node:
                unsupported.add('on_error')
            if kind == 'action_group' and 'timeout_seconds' in node:
                unsupported.add('timeout_seconds')
            if unsupported:
                errors.append(f'{path} 不支持字段: {", ".join(sorted(unsupported))}')
        if kind == 'trigger':
            trigger = node.get('trigger')
            if not isinstance(trigger, dict) or not isinstance(trigger.get('type'), str) or not isinstance(trigger.get('params', {}), dict):
                errors.append(f'{path}.trigger 必须是插件触发器')
            else:
                leaf_id = trigger.get('binding_id')
                if not isinstance(leaf_id, str) or not _BINDING_ID_RE.fullmatch(leaf_id) or not leaf_id.startswith('t_') or leaf_id in identities:
                    errors.append(f'{path}.trigger.binding_id 无效或重复')
                if isinstance(leaf_id, str):
                    identities.add(leaf_id)
            if node.get('occurrence', 'occurred') not in ('occurred', 'absent'):
                errors.append(f'{path}.occurrence 无效')
            if node.get('absence_scope', 'run') not in ('run', 'window'):
                errors.append(f'{path}.absence_scope 无效')
            if node.get('repeat', 'once_until_seen') not in ('once_until_seen', 'every_window'):
                errors.append(f'{path}.repeat 无效')
        if kind == 'logic' and node.get('op') not in ('all', 'any', 'not'):
            errors.append(f'{path}.op 必须是 all、any 或 not')
        if kind == 'condition':
            predicate = node.get('predicate')
            if not isinstance(predicate, dict) or 'children' in predicate or predicate.get('op') not in ('eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'is_true', 'is_false', 'exists', 'not_exists', 'run_status'):
                errors.append(f'{path}.predicate 必须是数据比较或自动化状态判断')
        if kind == 'event_group':
            if node.get('mode', 'immediate') not in ('immediate', 'wait'):
                errors.append(f'{path}.mode 必须是 immediate 或 wait')
            if 'timeout_seconds' in node and node.get('mode') != 'wait':
                errors.append(f'{path} 只有等待事件组支持 timeout_seconds')
            if 'timeout_seconds' in node and (not _finite_number(node['timeout_seconds']) or not 0 < node['timeout_seconds'] <= 86400):
                errors.append(f'{path}.timeout_seconds 必须是 0 到 86400 之间的秒数')
            if not isinstance(node.get('group_type'), str) or node['group_type'] not in GROUP_TYPES:
                errors.append(f'{path}.group_type 必须是 if、all、any 或 not')
            values = node.get('branches', [])
            if not isinstance(values, list):
                errors.append(f'{path}.branches 必须是数组')
                values = []
            ports, else_count = set(), 0
            for branch in values:
                if not isinstance(branch, dict) or not isinstance(branch.get('id'), str) or not branch['id'].startswith('b_'):
                    errors.append(f'{path} 的分支身份无效')
                    continue
                if branch['id'] in ports:
                    errors.append(f'{path} 的分支身份重复')
                ports.add(branch['id'])
                if 'negated' in branch and not isinstance(branch['negated'], bool):
                    errors.append(f'{path} 的分支 negated 必须是布尔值')
                if branch.get('else'):
                    else_count += 1
                elif else_count:
                    errors.append(f'{path} 的 ELSE 必须位于最后')
                if any(field in branch for field in ('condition', 'condition_graph', 'then')):
                    errors.append(f'{path} 的分支使用了已移除的条件模型')
            if node.get('group_type') == 'if' and (else_count != 1 or len(values) < 2):
                errors.append(f'{path} 必须包含 IF 和唯一 ELSE')
            branches[identity] = {branch['id']: branch for branch in values}
        for field in ('duration_seconds', 'within_seconds'):
            if field in node:
                value = node[field]
                if not _finite_number(value) or not 0 < value <= 86400:
                    errors.append(f'{path}.{field} 必须是 0 到 86400 之间的秒数')
        if 'negated' in node and not isinstance(node['negated'], bool):
            errors.append(f'{path}.negated 必须是布尔值')
        op = node.get('op', node.get('group_type'))
        for option, owner, allowed in (
            ('consume', 'all', ('matched', 'all', 'none')),
            ('pick', 'any', ('latest', 'first')),
            ('repeat', 'not', ('once_until_seen', 'every_window')),
        ):
            trigger_repeat = kind == 'trigger' and option == 'repeat'
            if option in node and not trigger_repeat and (op != owner or node[option] not in allowed):
                errors.append(f'{path}.{option} 不是该条件组支持的选项')
        if 'ttl_seconds' in node and (op != 'all' or not _finite_number(node['ttl_seconds']) or node['ttl_seconds'] <= 0):
            errors.append(f'{path}.ttl_seconds 只用于 AND，必须是大于零的有限秒数')
        if node.get('parent') is not None and not isinstance(node['parent'], str):
            errors.append(f'{path}.parent 必须是所属组的节点身份')
        if 'position' in node:
            position = node['position']
            if not isinstance(position, dict) or any(not _finite_number(position.get(key)) for key in ('x', 'y')):
                errors.append(f'{path}.position 必须是有限数字坐标')
    for identity, node in nodes.items():
        visited, parent = {identity}, node.get('parent')
        while isinstance(parent, str):
            if parent not in nodes or nodes[parent].get('type') not in ('event_group', 'action_group'):
                errors.append(f'节点 {identity} 的所属组不存在')
                break
            if parent in visited:
                errors.append('事件组不能包含自身或祖先组')
                break
            visited.add(parent)
            parent = nodes[parent].get('parent')
    adjacency, outlets, pairs, inputs = defaultdict(list), set(), set(), defaultdict(int)
    def action_scope(identity):
        parent = nodes[identity].get('parent')
        seen = set()
        while isinstance(parent, str) and parent in nodes and parent not in seen:
            seen.add(parent)
            if nodes[parent]['type'] == 'action_group':
                return parent
            parent = nodes[parent].get('parent')
        return None
    for index, edge in enumerate(graph['edges']):
        path = f'workflow.edges[{index}]'
        if not isinstance(edge, dict) or not isinstance(edge.get('source'), str) or not isinstance(edge.get('target'), str) or edge['source'] not in nodes or edge['target'] not in nodes:
            errors.append(f'{path} 引用的节点不存在')
            continue
        source, target = nodes[edge['source']], nodes[edge['target']]
        channel = edge.get('channel')
        port, input_port = edge.get('port'), edge.get('target_port', 'input')
        if not all(isinstance(value, str) for value in (port, input_port, channel)):
            errors.append(f'{path} 的端口和 channel 必须是字符串')
            continue
        key = edge['source'], port, edge['target'], input_port, channel
        if key in pairs:
            errors.append(f'{path} 连线重复')
        pairs.add(key)
        if channel not in ('control', 'condition'):
            errors.append(f'{path}.channel 必须是 control 或 condition')
        valid_ports = branches.get(edge['source'], {}) if source.get('group_type') == 'if' else ('event',) if source['type'] == 'trigger' else ('result',) if source['type'] in ('logic', 'condition', 'event_group') else ('next', 'entry') if source['type'] == 'action_group' else () if source['type'] == 'end' else ('next',) if source['type'] == 'merge' else ('next', 'failure')
        if port not in valid_ports:
            errors.append(f'{path} 的来源端口不存在')
        if channel == 'condition':
            if source.get('type') not in ('trigger', 'condition', 'logic', 'event_group') or target.get('type') not in ('logic', 'event_group'):
                errors.append(f'{path} 的条件端口类型不匹配')
            if target.get('type') == 'event_group' and target.get('group_type') == 'if':
                branch = branches.get(edge['target'], {}).get(input_port)
                if branch is None or branch.get('else'):
                    errors.append(f'{path} 不能把条件接入 ELSE 或不存在的分支')
            elif input_port != 'input':
                errors.append(f'{path} 的目标条件端口不存在')
            inputs[edge['target'], input_port] += 1
        else:
            outlet = edge['source'], port
            if outlet in outlets:
                errors.append(f'{path} 同一控制出口只能接一个目标')
            outlets.add(outlet)
            if target.get('type') in ('logic', 'condition'):
                errors.append(f'{path} 控制流不能接入条件节点')
            if source.get('type') == 'event_group' and source.get('group_type') == 'if' and port not in branches.get(edge['source'], {}):
                errors.append(f'{path} 控制出口不存在')
            if input_port != 'input':
                errors.append(f'{path} 控制流只能连接执行入口')
            if port == 'entry':
                if source['type'] != 'action_group' or action_scope(edge['target']) != edge['source']:
                    errors.append(f'{path} 动作组入口只能连接组内节点')
            elif action_scope(edge['source']) != action_scope(edge['target']):
                errors.append(f'{path} 控制流不能跨越动作组边界')
            if port == 'failure' and target['type'] != 'action_group':
                errors.append(f'{path} 失败出口只能连接动作组')
        adjacency[edge['source']].append(edge['target'])
    handlers = {edge['target'] for edge in graph['edges'] if isinstance(edge, dict)
                and edge.get('channel') == 'control' and edge.get('port') == 'failure'
                and isinstance(edge.get('target'), str)}
    for edge in graph['edges']:
        if not isinstance(edge, dict) or edge.get('channel') != 'control':
            continue
        if isinstance(edge.get('target'), str) and edge['target'] in handlers and edge.get('port') != 'failure':
            errors.append('补救动作组不能同时连接主流程入口')
        if isinstance(edge.get('source'), str) and edge['source'] in handlers and edge.get('port') == 'next':
            errors.append('补救动作组按停止／继续设置返回，不能连接对外 next 出口')
    for (identity, port), count in inputs.items():
        node = nodes[identity]
        if count > 1 and node.get('group_type') == 'if':
            errors.append('IF 或 ELIF 只能直接连接一个条件，请先组合 AND 或 OR')
        if count > 1 and (node.get('group_type') == 'not' or node.get('op') == 'not'):
            errors.append('NOT 只能连接一个条件')
    for identity, values in branches.items():
        for port, branch in values.items():
            if not branch.get('else') and (identity, port) in outlets and not inputs[identity, port]:
                errors.append('IF 或 ELIF 已连接动作，请先连接该分支的条件')
    visited, visiting = set(), set()
    def visit(identity):
        if identity in visiting:
            errors.append('workflow 连线不能形成循环')
            return
        if identity in visited:
            return
        visiting.add(identity)
        for child in adjacency[identity]:
            visit(child)
        visiting.remove(identity)
        visited.add(identity)
    for identity in nodes:
        visit(identity)
    if not errors:
        program = CanvasProgram({'workflow': graph})
        active = program.active_nodes()
        for identity in active:
            if nodes[identity].get('mode') == 'wait' and not program.event_dependencies(identity):
                errors.append('等待事件组必须连接触发器')
    return errors


class CanvasProgram:
    __slots__ = ('nodes', 'edges', 'inputs', 'predecessors', 'successors', 'paths', 'scopes', 'control_sources')

    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise TypeError('编译后的图索引不能修改')
        object.__setattr__(self, name, value)

    def __init__(self, rule):
        graph = rule['workflow']
        self.nodes = MappingProxyType({node['binding_id']: node for node in graph['nodes']})
        self.edges = tuple(graph['edges'])
        conditions, predecessors, successors = defaultdict(list), defaultdict(list), {}
        for edge in self.edges:
            if edge['channel'] == 'condition':
                conditions[edge['target'], edge.get('target_port', 'input')].append(edge)
            else:
                successors[edge['source'], edge['port']] = edge['target']
                predecessors[edge['target']].append(edge)
        self.inputs = MappingProxyType({key: tuple(value) for key, value in conditions.items()})
        self.predecessors = MappingProxyType({key: tuple(value) for key, value in predecessors.items()})
        self.successors = MappingProxyType(successors)
        self.control_sources = frozenset(source for source, _port in successors)
        self.paths = MappingProxyType({identity: self._group_path(identity) for identity in self.nodes})
        self.scopes = MappingProxyType({identity: next((parent for parent in reversed(self.paths[identity])
            if self.nodes[parent]['type'] == 'action_group'), None) for identity in self.nodes})

    def _group_path(self, identity):
        path = []
        parent = self.nodes[identity].get('parent')
        while parent is not None:
            path.append(parent)
            parent = self.nodes[parent].get('parent')
        return tuple(reversed(path))

    def operands(self, identity, port='input'):
        return self.inputs.get((identity, port), ())

    def dependencies(self, identity):
        node = self.nodes[identity]
        if node['type'] == 'action_group':
            entry = self.successors.get((identity, 'entry'))
            return (entry,) if entry is not None else ()
        if node.get('group_type') == 'if':
            return tuple(edge['source'] for branch in node['branches'] if not branch.get('else')
                for edge in self.operands(identity, branch['id']))
        return tuple(edge['source'] for edge in self.operands(identity))

    def event_dependencies(self, identity):
        result, pending, seen = set(), [identity], set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if self.nodes[current]['type'] == 'trigger':
                result.add(current)
            else:
                pending.extend(self.dependencies(current))
        return frozenset(result)

    def roots(self):
        return tuple(sorted(identity for identity, node in self.nodes.items()
            if self.scopes[identity] is None and not self.predecessors.get(identity)
            and identity in self.control_sources
            and self.event_dependencies(identity)))

    def reachable(self, entry, stop=None):
        result, pending = set(), [entry]
        while pending:
            current = pending.pop()
            if current in result or current is None:
                continue
            result.add(current)
            if current != stop:
                pending.extend(target for (source, _port), target in self.successors.items() if source == current)
        return frozenset(result)

    def active_nodes(self):
        active = set(identity for root in self.roots() for identity in self.reachable(root))
        pending = list(active)
        while pending:
            for dependency in self.dependencies(pending.pop()):
                if dependency not in active:
                    active.add(dependency)
                    pending.append(dependency)
        return frozenset(active)

    def evaluate(self, identity, context, *, port=None, status_lookup=None, occurred=None, records=None):
        from notmyfault.core.predicates import evaluate_predicate, event_occurrences
        occurred = event_occurrences(context) if occurred is None else occurred
        memo = {}

        def value(current, output=None):
            key = current, output
            if key in memo:
                return memo[key]
            node = self.nodes[current]
            kind = node['type']
            if kind == 'trigger':
                trigger_id = node['trigger']['binding_id']
                if node.get('occurrence') == 'absent':
                    if node.get('absence_scope') == 'window':
                        observation = context.get('_event_observation')
                        if observation is None:
                            raise ValueError('连续未发生判断需要当前运行的事件观察')
                        result = observation.absent_for(trigger_id, node.get('duration_seconds', 10))
                    else:
                        result = trigger_id not in occurred
                else:
                    result = trigger_id in occurred
            elif kind == 'condition':
                try:
                    result = evaluate_predicate(node['predicate'], context, f'{current}.predicate', status_lookup=status_lookup)
                except (ValueError, TypeError) as error:
                    if records is not None:
                        records.append({'node_id': current, 'op': node['predicate']['op'],
                                        'error': {'code': getattr(error, 'code', 'condition_evaluation_failed')}})
                    raise
            elif kind == 'action_group':
                entry = self.successors.get((current, 'entry'))
                result = value(entry) if entry is not None else False
            elif node.get('group_type') == 'if':
                selected = choose(current)
                result = selected == output if output is not None else selected is not None and not next(
                    branch for branch in node['branches'] if branch['id'] == selected).get('else')
            else:
                operands = self.operands(current)
                op = node.get('op', node.get('group_type'))
                if not operands:
                    result = False
                elif op == 'all':
                    result = all(value(edge['source'], edge['port']) for edge in operands)
                elif op == 'any':
                    result = any(value(edge['source'], edge['port']) for edge in operands)
                elif op == 'not':
                    result = not value(operands[0]['source'], operands[0]['port'])
                else:
                    raise ValueError('条件连线引用了不可求值的图块')
            if node.get('negated'):
                result = not result
            memo[key] = result
            if records is not None:
                records.append({'node_id': current, 'op': node['predicate']['op'] if kind == 'condition' else node.get('op', node.get('group_type', kind)), 'result': result})
            return result

        def choose(current):
            node = self.nodes[current]
            for branch in node['branches']:
                if branch.get('else'):
                    return branch['id']
                operands = self.operands(current, branch['id'])
                if not operands:
                    continue
                try:
                    result = value(operands[0]['source'], operands[0]['port'])
                except (ValueError, TypeError) as error:
                    if records is not None:
                        records.append({'branch_id': branch['id'], 'error': {'code': getattr(error, 'code', 'condition_evaluation_failed')}})
                    raise
                if branch.get('negated'):
                    result = not result
                if records is not None:
                    records.append({'branch_id': branch['id'], 'result': result})
                if result:
                    return branch['id']
            return None

        node = self.nodes[identity]
        if node.get('group_type') == 'if' and port is None:
            return choose(identity)
        return value(identity, port)
