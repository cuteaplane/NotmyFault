from __future__ import annotations


def workflow_draft_schema(action_ids=None, trigger_ids=None):
    def node(properties, required):
        return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}

    def plugin_type(identities):
        return {'type': 'string', **({'enum': identities} if identities is not None else {})}

    identity = {'type': 'string', 'pattern': '^a_[a-z0-9_]{6,64}$'}
    common = {'binding_id': identity, 'parent': identity, 'name': {'type': 'string'}, 'position': node({'x': {'type': 'number'}, 'y': {'type': 'number'}}, ['x', 'y'])}
    duration = {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 86400}
    leaf = node({'binding_id': {'type': 'string', 'pattern': '^t_[a-z0-9_]{6,64}$'}, 'type': plugin_type(trigger_ids), 'params': {'type': 'object'}}, ['binding_id', 'type', 'params'])
    trigger = node({**common, 'type': {'const': 'trigger'}, 'trigger': leaf, 'occurrence': {'enum': ['occurred', 'absent']}, 'absence_scope': {'enum': ['run', 'window']}, 'duration_seconds': duration, 'repeat': {'enum': ['once_until_seen', 'every_window']}}, ['binding_id', 'type', 'trigger'])
    condition = node({**common, 'type': {'const': 'condition'}, 'negated': {'type': 'boolean'}, 'predicate': {'type': 'object'}}, ['binding_id', 'type', 'predicate'])
    timing = {'consume': {'enum': ['matched', 'all', 'none']}, 'pick': {'enum': ['latest', 'first']}, 'ttl_seconds': {'type': 'number', 'exclusiveMinimum': 0}, 'repeat': {'enum': ['once_until_seen', 'every_window']}}
    logic = node({**common, **timing, 'type': {'const': 'logic'}, 'op': {'enum': ['all', 'any', 'not']}, 'negated': {'type': 'boolean'}, 'within_seconds': duration, 'duration_seconds': duration}, ['binding_id', 'type', 'op'])
    branch = node({'id': {'type': 'string', 'pattern': '^b_[a-z0-9_]+$'}, 'label': {'type': 'string'}, 'else': {'const': True}, 'negated': {'type': 'boolean'}}, ['id'])
    event_group = node({**common, **timing, 'type': {'const': 'event_group'}, 'group_type': {'enum': ['if', 'all', 'any', 'not']}, 'branches': {'type': 'array', 'items': branch}, 'negated': {'type': 'boolean'}, 'within_seconds': duration, 'duration_seconds': duration, 'mode': {'enum': ['immediate', 'wait']}, 'timeout_seconds': duration}, ['binding_id', 'type', 'group_type'])
    action_group = node({**common, 'type': {'const': 'action_group'}, 'exports': {'type': 'object'}}, ['binding_id', 'type'])
    action = node({**common, 'type': plugin_type(action_ids), 'params': {'type': 'object'}, 'retry': {'type': 'integer', 'minimum': 0, 'maximum': 3}, 'retry_delay_seconds': {'type': 'number', 'minimum': 0, 'maximum': 3600}, 'retry_backoff': {'enum': ['fixed', 'exponential']}, 'timeout_seconds': duration, 'on_error': {'enum': ['stop', 'continue']}}, ['binding_id', 'type', 'params'])
    assignment = node({**common, 'type': {'const': 'set_variable'}, 'variable': {'type': 'string'}, 'value': {}, 'on_error': {'enum': ['stop', 'continue']}}, ['binding_id', 'type', 'variable', 'value'])
    merge = node({**common, 'type': {'const': 'merge'}}, ['binding_id', 'type'])
    end = node({**common, 'type': {'const': 'end'}}, ['binding_id', 'type'])
    workflow = node({'entry': {'type': 'null'}, 'nodes': {'type': 'array', 'items': {'$ref': '#/$defs/workflow_node'}}, 'edges': {'type': 'array', 'items': node({'source': identity, 'target': identity, 'port': {'type': 'string'}, 'target_port': {'type': 'string'}, 'channel': {'enum': ['control', 'condition']}}, ['source', 'target', 'port', 'channel'])}}, ['nodes', 'edges'])
    return {'$ref': '#/$defs/workflow'}, {'workflow': workflow, 'action': action, 'workflow_node': {'anyOf': [trigger, condition, logic, event_group, action_group, action, assignment, merge, end]}}
