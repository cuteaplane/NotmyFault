from __future__ import annotations

import copy
import math
from collections import defaultdict


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
        if kind == 'if' or any(field in node for field in ('activation', 'condition_graph', 'workflow', 'then', 'elif', 'else', 'triggers')):
            errors.append(f'{path} 使用了已移除的条件模型')
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
            if not isinstance(predicate, dict) or predicate.get('op') not in ('eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'is_true', 'is_false', 'exists', 'not_exists', 'run_status'):
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
        valid_ports = branches.get(edge['source'], {}) if source.get('group_type') == 'if' else ('event',) if source['type'] == 'trigger' else ('result',) if source['type'] in ('logic', 'condition', 'event_group') else ('next', 'entry') if source['type'] == 'action_group' else ('next',)
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
        adjacency[edge['source']].append(edge['target'])
    for (identity, port), count in inputs.items():
        node = nodes[identity]
        if count > 1 and node.get('group_type') == 'if':
            errors.append('IF 或 ELIF 只能直接连接一个条件，请先组合 AND 或 OR')
        if count > 1 and (node.get('group_type') == 'not' or node.get('op') == 'not'):
            errors.append('NOT 只能连接一个条件')
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
    return errors


class CanvasProgram:
    def __init__(self, rule):
        self.rule = rule
        self.nodes = {node['binding_id']: node for node in canvas_nodes(rule)}
        self.edges = rule['workflow']['edges']
        self.inputs = defaultdict(list)
        self.controls = defaultdict(list)
        for edge in self.edges:
            (self.inputs if edge['channel'] == 'condition' else self.controls)[edge['target']].append(edge)

    def children(self, identity, port=None):
        return [edge['source'] for edge in self.inputs[identity] if port is None or edge.get('target_port', 'input') == port]

    def operands(self, identity, port=None):
        return [edge for edge in self.inputs[identity] if port is None or edge.get('target_port', 'input') == port]

    def operand_predicate(self, edge, visiting=frozenset()):
        source = self.nodes[edge['source']]
        port = edge['port'] if source.get('group_type') == 'if' else None
        return self.predicate(edge['source'], port, visiting)

    def scope(self, identity):
        parent = self.nodes[identity].get('parent')
        while parent is not None:
            node = self.nodes[parent]
            if node['type'] == 'action_group':
                return parent
            parent = node.get('parent')
        return None

    def predicate(self, identity, port=None, visiting=frozenset()):
        if identity in visiting:
            raise ValueError('条件连线不能形成循环')
        visiting = visiting | {identity}
        node, kind = self.nodes[identity], self.nodes[identity]['type']
        if kind == 'trigger':
            result = {'op': 'event_occurred', 'trigger_id': node['trigger']['binding_id'], 'expected': node.get('occurrence', 'occurred') == 'occurred'}
            if node.get('absence_scope') == 'window' and node.get('occurrence') == 'absent':
                result['within_seconds'] = node.get('duration_seconds', 10)
        elif kind == 'condition':
            result = copy.deepcopy(node['predicate'])
        elif kind == 'action_group':
            entry = next((edge['target'] for edge in self.edges if edge['source'] == identity and edge['port'] == 'entry' and edge['channel'] == 'control'), None)
            result = self.predicate(entry, visiting=visiting) if entry else {'op': 'is_true', 'left': False}
        elif kind == 'event_group' and node['group_type'] == 'if':
            values = []
            result = None
            for branch in node['branches']:
                incoming = self.operands(identity, branch['id'])
                if branch.get('else'):
                    if port == branch['id']:
                        result = {'op': 'not', 'children': [{'op': 'any', 'children': values}]} if values else {'op': 'is_true', 'left': True}
                    continue
                if incoming:
                    value = self.operand_predicate(incoming[0], visiting)
                    value = {'op': 'not', 'children': [value]} if branch.get('negated') else value
                    if port == branch['id']:
                        result = {'op': 'all', 'children': [{'op': 'not', 'children': [{'op': 'any', 'children': list(values)}]}, value]} if values else value
                    values.append(value)
            if result is None:
                result = {'op': 'any', 'children': values} if port is None and values else {'op': 'is_true', 'left': False}
        else:
            op = node.get('op', node.get('group_type'))
            values = [self.operand_predicate(edge, visiting) for edge in self.operands(identity)]
            if not values:
                result = {'op': 'is_true', 'left': False}
            elif op == 'not' and len(values) != 1:
                raise ValueError('NOT 只能连接一个条件')
            else:
                result = {'op': op, 'children': values}
        if node.get('negated'):
            result = {'op': 'not', 'children': [result]}
        return result

    def activation(self, identity, port=None, visiting=frozenset()):
        if identity in visiting:
            raise ValueError('启动条件连线不能形成循环')
        visiting = visiting | {identity}
        node, kind = self.nodes[identity], self.nodes[identity]['type']
        if kind == 'trigger':
            trigger = copy.deepcopy(node['trigger'])
            if node.get('occurrence') == 'absent':
                return {'op': 'not', 'children': [trigger], 'within_seconds': node.get('duration_seconds', 10), 'repeat': node.get('repeat', 'once_until_seen')}
            return trigger
        if kind == 'condition':
            return None
        if kind == 'action_group':
            entry = next((edge['target'] for edge in self.edges if edge['source'] == identity and edge['port'] == 'entry' and edge['channel'] == 'control'), None)
            return self.activation(entry, visiting=visiting) if entry else None
        if kind == 'event_group' and node['group_type'] == 'if':
            children = [self.activation(child, visiting=visiting) for branch in node['branches'] if not branch.get('else') for child in self.children(identity, branch['id'])]
            op = 'any'
        else:
            children = [self.activation(child, visiting=visiting) for child in self.children(identity)]
            op = node.get('op', node.get('group_type'))
        children = [child for child in children if child is not None]
        if not children:
            return None
        if op == 'not' or node.get('negated'):
            if len(children) == 1 and 'type' in children[0]:
                return {'op': 'not', 'children': children, 'within_seconds': node.get('duration_seconds', 10), 'repeat': node.get('repeat', 'once_until_seen')}
            op = 'any'
        result = children[0] if len(children) == 1 else {'op': op, 'children': children}
        if len(children) > 1 and op == 'all':
            result.update(within_seconds=node.get('within_seconds', 10), consume=node.get('consume', 'matched'))
        return result

    def roots(self):
        roots = []
        for edge in self.edges:
            if edge['channel'] != 'control':
                continue
            source = self.nodes[edge['source']]
            if self.scope(source['binding_id']) is not None:
                continue
            if self.controls[source['binding_id']]:
                continue
            if source['type'] in ('trigger', 'logic', 'event_group', 'action_group') and self.activation(source['binding_id']) is not None:
                roots.append(source['binding_id'])
        return list(dict.fromkeys(roots))

    def lower(self, roots=None, scope=None):
        from notmyfault.core.workflow_graph import predicate_condition_graph
        roots = self.roots() if roots is None else roots
        result = {key: copy.deepcopy(value) for key, value in self.rule.items() if key != 'workflow'}
        graph = {'entry': None, 'nodes': [], 'edges': []}
        result['workflow'] = graph
        used, pending = set(), list(roots)
        while pending:
            identity = pending.pop()
            if identity in used:
                continue
            used.add(identity)
            pending.extend(edge['target'] for edge in self.edges if edge['channel'] == 'control' and edge['source'] == identity and edge['port'] != 'entry' and self.scope(edge['target']) == scope)
        if not roots:
            return result
        for identity in self.nodes:
            if identity not in used:
                continue
            raw = self.nodes[identity]
            kind = raw['type']
            node = {key: copy.deepcopy(value) for key, value in raw.items() if key not in ('parent', 'position', 'group_type', 'negated')}
            if kind == 'action_group':
                entry = [edge['target'] for edge in self.edges if edge['source'] == identity and edge['port'] == 'entry' and edge['channel'] == 'control']
                node['workflow'] = self.lower(entry, identity)['workflow']
            if kind in ('trigger', 'logic', 'event_group'):
                node = {'binding_id': identity, 'type': 'event_group', 'mode': 'immediate', 'name': raw.get('name', '')}
                if kind == 'event_group' and raw['group_type'] == 'if':
                    branches = []
                    for branch in raw['branches']:
                        if branch.get('else'):
                            branches.append(copy.deepcopy(branch))
                            continue
                        incoming = self.operands(identity, branch['id'])
                        if len(incoming) > 1:
                            raise ValueError('IF 或 ELIF 只能直接连接一个条件，请先组合 AND 或 OR')
                        if incoming:
                            value = self.operand_predicate(incoming[0])
                            if branch.get('negated'):
                                value = {'op': 'not', 'children': [value]}
                            branches.append({**branch, 'condition_graph': predicate_condition_graph(value, seed=identity + branch['id'])})
                        elif any(edge['source'] == identity and edge['port'] == branch['id'] and edge['channel'] == 'control' for edge in self.edges):
                            raise ValueError('IF 或 ELIF 已连接动作，请先连接该分支的条件')
                    node['branches'] = branches
                else:
                    node['branches'] = [{'id': 'b_pass_' + identity[2:], 'condition_graph': predicate_condition_graph(self.predicate(identity), seed=identity)}, {'id': 'b_else_' + identity[2:], 'else': True}]
                leaves = []
                def collect(value):
                    if value.get('op') == 'event_occurred':
                        leaves.append(value['trigger_id'])
                    for child in value.get('children', []):
                        collect(child)
                for branch in node['branches']:
                    if not branch.get('else'):
                        from notmyfault.core.workflow_graph import branch_predicate
                        collect(branch_predicate(branch))
                node['triggers'] = [copy.deepcopy(candidate['trigger']) for candidate in self.nodes.values() if candidate['type'] == 'trigger' and candidate['trigger']['binding_id'] in leaves]
                waiting = raw.get('mode') == 'wait' or kind == 'trigger' and (scope is not None or self.controls[identity])
                if identity in roots or waiting:
                    node['activation'] = self.activation(identity)
                if waiting:
                    if node['activation'] is None:
                        raise ValueError('等待事件组必须连接触发器')
                    node['mode'] = 'wait'
                    if 'timeout_seconds' in raw:
                        node['timeout_seconds'] = raw['timeout_seconds']
            graph['nodes'].append(node)
            outgoing = [edge for edge in self.edges if edge['source'] == identity and edge['channel'] == 'control' and edge['port'] != 'entry' and self.scope(edge['target']) == scope]
            for edge in outgoing:
                port = edge['port']
                if kind in ('trigger', 'logic') or kind == 'event_group' and raw['group_type'] != 'if':
                    port = 'b_pass_' + identity[2:]
                graph['edges'].append({'source': identity, 'port': port, 'target': edge['target']})
            available = [branch['id'] for branch in node.get('branches', [])] if node['type'] == 'event_group' else ['next']
            for port in available:
                if not any(edge['source'] == identity and edge['port'] == port for edge in graph['edges']):
                    graph['edges'].append({'source': identity, 'port': port, 'target': None})
        if len(roots) == 1:
            graph['entry'] = roots[0]
        else:
            virtual_id = 'a_canvas_entry'
            if virtual_id in self.nodes:
                raise ValueError('a_canvas_entry 是编译器保留身份')
            virtual = {'binding_id': virtual_id, 'type': 'event_group', 'mode': 'immediate', 'triggers': trigger_leaves(self.rule), 'branches': [], 'activation': {'op': 'any', 'children': [self.activation(identity) for identity in roots]}}
            for index, identity in enumerate(roots):
                branch_id = f'b_entry_{index}'
                virtual['branches'].append({'id': branch_id, 'condition_graph': predicate_condition_graph(self.predicate(identity), seed=branch_id)})
                graph['edges'].append({'source': virtual_id, 'port': branch_id, 'target': identity})
            virtual['branches'].append({'id': 'b_entry_else', 'else': True})
            graph['edges'].append({'source': virtual_id, 'port': 'b_entry_else', 'target': None})
            graph['nodes'].insert(0, virtual)
            graph['entry'] = virtual_id
        return result


def lower_canvas_rule(rule):
    errors = validate_canvas_graph(rule.get('workflow'))
    if errors:
        raise ValueError('；'.join(errors))
    return CanvasProgram(rule).lower()
