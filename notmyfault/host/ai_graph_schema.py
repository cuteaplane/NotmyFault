from __future__ import annotations


def workflow_draft_schema(action_ids=None, trigger_ids=None):
    def plugin_type(identities):
        return {"type": "string", **({"enum": identities} if identities is not None else {})}

    def node(properties, required):
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    identity = {"type": "string", "pattern": "^a_[a-z0-9_]{6,64}$"}
    reference = {"type": "object"}
    trigger = node({
        "binding_id": {"type": "string", "pattern": "^t_[a-z0-9_]{6,64}$"},
        "type": plugin_type(trigger_ids), "params": {"type": "object"},
    }, ["binding_id", "type", "params"])
    condition = node({
        "root": {"type": "string"},
        "nodes": {"type": "array", "items": {"anyOf": [
            node({"id": {"type": "string"}, "predicate": reference}, ["id", "predicate"]),
            node({"id": {"type": "string"}, "op": {"enum": ["all", "any", "not"]}}, ["id", "op"]),
        ]}},
        "edges": {"type": "array", "items": node({
            "source": {"type": "string"}, "target": {"type": "string"},
            "order": {"type": "integer", "minimum": 0},
        }, ["source", "target", "order"])},
    }, ["root", "nodes", "edges"])
    branch = node({
        "id": {"type": "string"}, "label": {"type": "string"},
        "condition_graph": condition, "else": {"const": True},
    }, ["id"])
    workflow = node({
        "entry": {"type": ["string", "null"]},
        "nodes": {"type": "array", "items": {"$ref": "#/$defs/workflow_node"}},
        "edges": {"type": "array", "items": node({
            "source": {"type": "string"}, "port": {"type": "string"},
            "target": {"type": ["string", "null"]},
        }, ["source", "port", "target"])},
    }, ["entry", "nodes", "edges"])
    action = node({
        "binding_id": identity, "type": plugin_type(action_ids), "params": {"type": "object"},
        "retry": {"type": "integer", "minimum": 0, "maximum": 3}, "retry_delay_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
        "retry_backoff": {"enum": ["fixed", "exponential"]}, "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400},
        "on_error": {"enum": ["stop", "continue"]},
        "failure_actions": {"type": "array", "items": {"$ref": "#/$defs/workflow_node"}},
    }, ["binding_id", "type", "params"])
    assignment = node({
        "binding_id": identity, "type": {"const": "set_variable"},
        "variable": {"type": "string"}, "value": {}, "on_error": {"enum": ["stop", "continue"]},
    }, ["binding_id", "type", "variable", "value"])
    event_group = node({
        "binding_id": identity, "type": {"const": "event_group"}, "label": {"type": "string"}, "name": {"type": "string"},
        "mode": {"enum": ["immediate", "wait"]}, "activation": {"type": "object"},
        "triggers": {"type": "array", "items": trigger}, "branches": {"type": "array", "items": branch, "minItems": 1},
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
    }, ["binding_id", "type", "mode", "branches"])
    action_group = node({
        "binding_id": identity, "type": {"const": "action_group"}, "label": {"type": "string"}, "name": {"type": "string"},
        "workflow": {"$ref": "#/$defs/workflow"}, "exports": {"type": "object"},
    }, ["binding_id", "type", "workflow"])
    terminal = node({"binding_id": identity, "type": {"enum": ["merge", "end"]}}, ["binding_id", "type"])
    return {"$ref": "#/$defs/workflow"}, {
        "workflow": workflow,
        "workflow_node": {"anyOf": [action, assignment, event_group, action_group, terminal]},
    }
