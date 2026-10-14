import sys
import logging
import threading
import uuid
import weakref
from collections import deque
from typing import Any, Callable, Dict, Optional

from notmyfault.core.logging import engine_error, engine_warn
from notmyfault.core.bindings import BindingResolutionError
from notmyfault.values import hold_value
from notmyfault.core.data_types import DataTypeError, normalize_fields
from notmyfault.core.workflow import build_context
from notmyfault.core.run_context import RunState
from notmyfault.core.runtime_rules import PreparedRules, rule_key
from notmyfault.core.conditions import config_fingerprint
from notmyfault.core.event_observation import RunEventObservation
from notmyfault.core.workflow_graph import iter_rule_actions, NATIVE_NODES, entry_event_group

# 两条规则互相监视会形成一条不会结束的触发链，链到这么深就丢弃这次运行
_MAX_RUN_CHAIN = 16
# 记住多少条已发起运行的深度，够覆盖同时在跑的链条
_RUN_DEPTH_MEMORY = 512
_logger = logging.getLogger(__name__)


class EventBus:
    """收事件、匹配规则、分发动作"""

    def __init__(
        self,
        snapshot_fn: Callable[[], PreparedRules],
        rules_lock: Any,
        triggers_meta_fn: Callable[[], Dict[str, Dict[str, Any]]],
        actions_meta_fn: Callable[[], Dict[str, Dict[str, Any]]],
        is_shutdown_fn: Callable[[], bool],
        safe_on_event: Callable[[str, Dict[str, Any]], None],
        execute_workflow_cb: Callable[..., None],
        scheduler_submit_fn: Callable[..., str] | None = None,
        alert_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        self._snapshot_fn = snapshot_fn
        self._rules_lock = rules_lock
        self._triggers_meta_fn = triggers_meta_fn
        self._actions_meta_fn = actions_meta_fn
        self._is_shutdown_fn = is_shutdown_fn
        self._safe_on_event = safe_on_event
        self._execute_workflow_cb = execute_workflow_cb
        # 有调度器时先决定跑不跑，没有就直接执行，旧测试路径走这里
        self._scheduler_submit_fn = scheduler_submit_fn
        self._alert_fn = alert_fn
        self._run_depths: Dict[str, int] = {}
        self._run_depth_order: deque = deque()
        self._chain_alerts: set = set()
        self._depth_lock = threading.Lock()
        self._observations = weakref.WeakSet()
        self._status_lookup = lambda identity: {"running": 0, "queued": 0, "last_status": None}

    def observe_events(self, compiled, occurred=()):
        snapshot = self._snapshot_fn()
        observation = RunEventObservation(compiled, occurred, clock=snapshot.runtime.clock, wake=snapshot.runtime._wake)
        with self._rules_lock:
            self._observations.add(observation)
        return observation

    def emit_event(
        self,
        event_type: str,
        event_payload: Dict[str, Any],
        instance: Optional[Dict[str, Any]] = None,
    ) -> None:
        """按事件版本匹配规则并分发事件"""
        if self._is_shutdown_fn():
            print(f"[EventBus] Engine正在关闭，忽略事件: [{event_type}]")
            return

        initial_snapshot = self._snapshot_fn()
        meta = self._triggers_meta_fn().get(event_type, {})
        semantic = meta.get("semantic", "oneshot")
        event_payload = hold_value(normalize_fields(
            event_payload, meta.get("outputs"), initial_snapshot.registry, wrap_sensitive=True,
        ))
        masked_payload = event_payload
        _logger.debug("收到事件 %s (%s): %r", event_type, semantic, masked_payload)

        depth = self._chain_depth(event_payload)
        if instance is not None and not instance.get("fingerprint"):
            instance = {**instance, "fingerprint": config_fingerprint(instance.get("config"))}
        dispatches = []
        with self._rules_lock:
            snapshot = self._snapshot_fn()
            for observation in tuple(self._observations):
                observation.record(event_type, event_payload, instance)
            due, matches = snapshot.runtime.process_event(
                ((compiled.rule_id, compiled.source) for compiled in snapshot.event_index.get(event_type, ())),
                event_type, event_payload, instance=instance,
            )
            for key, events in due:
                dispatches.append((snapshot.compiled[key].source, key, events, "absence", {}, {}, 0))
            for key, events in matches:
                dispatches.append((snapshot.compiled[key].source, key, events, event_type, event_payload, masked_payload, depth))
            dispatches = [(item, self.observe_events(snapshot.compiled[item[1]],
                          (event.get("binding_id") for event in item[2] if not event.get('absence')))) for item in dispatches]
            for _item, observation in dispatches:
                if observation is not None:
                    observation.seed(_item[2])
                    observation.record(event_type, event_payload, instance)
            if dispatches and snapshot.generation is not None:
                snapshot.generation.retain()
        try:
            for (rule, key, matched_events, kind, payload, masked, run_depth), observation in dispatches:
                self._dispatch(snapshot, rule, key, kind, payload, masked, matched_events, run_depth, observation)
        finally:
            if dispatches and snapshot.generation is not None:
                snapshot.generation.release()

    def _chain_depth(self, event_payload: Dict[str, Any]) -> int:
        """这次事件由哪次运行引起：0 表示不是链条里的下一环"""
        if not isinstance(event_payload, dict):
            return 0
        run_id = str(event_payload.get("run_id", "") or "").strip()
        if not run_id:
            return 0
        with self._depth_lock:
            return self._run_depths.get(run_id, 0) + 1

    def _remember_run_depth(self, run_id: str, depth: int) -> None:
        with self._depth_lock:
            if len(self._run_depth_order) >= _RUN_DEPTH_MEMORY:
                self._run_depths.pop(self._run_depth_order.popleft(), None)
            self._run_depth_order.append(run_id)
            self._run_depths[run_id] = depth

    def _drop_deep_chain(self, rule_id: str, rule_name: str, event_type: str, depth: int) -> None:
        """链条过深时丢弃运行，并只提醒一次"""
        engine_error(
            "run.chain_limited",
            rule=rule_name,
            rule_id=rule_id,
            event_type=event_type,
            depth=depth,
        )
        print(
            f"[EventBus] [!!] 规则 <{rule_name}> 的触发链已达 {depth} 层，"
            "丢弃这次运行，请检查是否有规则互相监视",
            file=sys.stderr,
        )
        self._safe_on_event(
            "run.chain_limited",
            {
                "rule_id": rule_id,
                "rule_name": rule_name,
                "event_type": event_type,
                "depth": depth,
            },
        )
        with self._depth_lock:
            first_time = rule_id not in self._chain_alerts
            self._chain_alerts.add(rule_id)
        if first_time and self._alert_fn is not None:
            self._alert_fn(
                "规则触发链过深",
                f"规则「{rule_name}」已连续被触发 {depth} 层，可能是两条规则互相监视。"
                "已丢弃这次运行，请在 Dashboard 检查规则。",
            )

    def process_timers(self, now=None):
        if self._is_shutdown_fn():
            return
        self._snapshot_fn()
        dispatches = []
        with self._rules_lock:
            snapshot = self._snapshot_fn()
            for observation in tuple(self._observations):
                observation.process_timers(now)
            for key, matched in snapshot.runtime.take_due(now):
                observation = self.observe_events(snapshot.compiled[key], (event.get("binding_id") for event in matched if not event.get('absence')))
                if observation is not None:
                    observation.seed(matched)
                dispatches.append((snapshot.compiled[key].source, key, matched, observation))
            if dispatches and snapshot.generation is not None:
                snapshot.generation.retain()
        try:
            for rule, key, matched, observation in dispatches:
                self._dispatch(snapshot, rule, key, "absence", {}, {}, matched, observation=observation)
        finally:
            if dispatches and snapshot.generation is not None:
                snapshot.generation.release()

    def next_observation_deadline(self):
        with self._rules_lock:
            values = [deadline for observation in tuple(self._observations) if (deadline := observation.next_deadline()) is not None]
            return min(values) if values else None

    def _dispatch(self, snapshot, rule, key, event_type, event_payload, masked_payload, matched_events, depth=0, observation=None):
        rule_id = rule.get("rule_id", "")
        rule_name = rule.get("name", "未命名规则")
        if depth > _MAX_RUN_CHAIN:
            self._drop_deep_chain(rule_id, rule_name, event_type, depth)
            return
        run_id = f"run_{uuid.uuid4().hex}"
        self._remember_run_depth(run_id, depth)
        _logger.debug("匹配规则 %s", rule_name)
        self._safe_on_event(
            "run.triggered",
            {
                "rule_id": rule_id,
                "run_id": run_id,
                "rule_name": rule_name,
                "event_type": event_type,
                "action_count": sum(node["type"] not in NATIVE_NODES - {"set_variable"} for node, _path in iter_rule_actions(rule)),
                "event_payload": masked_payload,
            },
        )
        context = RunState({"rule": {"id": rule_id, "name": rule_name}, "run": {"id": run_id},
                            "_generation": snapshot.generation, "_compiled_rule": snapshot.compiled[key],
                            "_event_observation": observation})

        def prepare(state):
            state.update(build_context(rule_name, event_type, event_payload, matched_events, rule_id, run_id))
            try:
                snapshot.prepare_context(rule, state)
            except (DataTypeError, BindingResolutionError) as error:
                state["_preparation_error"] = error.as_dict()

        program = snapshot.compiled[key].matcher_spec.graph
        root = next((event.get('root_id') for event in matched_events if event.get('root_id') in program.nodes), None)
        if root is None:
            roots = program.roots()
            root = roots[0] if roots else None
        context['_entry_root'] = root
        if root is not None:
            prepare(context)
            if not context.get('_preparation_error'):
                occurred = observation.apply_snapshot(context) if observation is not None else frozenset(
                    event.get('binding_id') for event in matched_events if not event.get('absence'))
                statuses = {}
                def lookup(identity):
                    if identity not in statuses:
                        statuses[identity] = self._status_lookup(identity)
                    return statuses[identity]
                choices, evaluated = {}, {}
                current = root
                try:
                    while current is not None:
                        node = program.nodes[current]
                        if node['type'] == 'action_group':
                            current = program.successors.get((current, 'entry'))
                            continue
                        records = evaluated[current] = []
                        if node['type'] == 'trigger':
                            choices[current] = 'event'
                        else:
                            from notmyfault.core.workflow_graph import evaluate_group_branches
                            choices[current] = evaluate_group_branches(node, context, records, program=program,
                                status_lookup=lookup, occurred=occurred)
                        break
                    context['_entry_choices'] = choices
                    context['_entry_evaluated_nodes'] = evaluated
                except (ValueError, TypeError) as error:
                    context['_preparation_error'] = {'code': 'condition_evaluation_failed', 'message': str(error)}
        else:
            context.defer_preparation(prepare)
        if self._scheduler_submit_fn is not None:
            self._scheduler_submit_fn(key, rule, rule_name, context)
        else:
            self._execute_workflow_cb(key, rule, rule_name, context)

    def call_notmyfault(self, event_data: Dict[str, Any]) -> None:
        """接收触发器线程推送的外部事件"""
        event_type = event_data.get("trigger_id")
        event_payload = event_data.get("triggered_params", {})
        if not isinstance(event_type, str) or not isinstance(event_payload, dict):
            engine_warn("忽略格式无效的外部事件")
            return
        self.emit_event(event_type, event_payload)
