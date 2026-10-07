import sys
import threading
import uuid
from collections import deque
from typing import Any, Callable, Dict, Optional

from notmyfault.core.logging import engine_error, engine_warn
from notmyfault.core.bindings import BindingResolutionError
from notmyfault.core.data_types import DataTypeError
from notmyfault.core.workflow import build_context
from notmyfault.core.runtime_rules import PreparedRules, rule_key
from notmyfault.core.conditions import config_fingerprint

# 两条规则互相监视会形成一条不会结束的触发链，链到这么深就丢弃这次运行
_MAX_RUN_CHAIN = 16
# 记住多少条已发起运行的深度，够覆盖同时在跑的链条
_RUN_DEPTH_MEMORY = 512


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

        semantic = self._triggers_meta_fn().get(event_type, {}).get("semantic", "oneshot")
        # 声明 sensitive 的输出字段不写明文，日志和事件推送都用掉过掩码的副本
        masked_payload = self._mask_event_payload(event_type, event_payload)
        print(
            f"[EventBus] 收到广播事件: [{event_type}] ({semantic}) -> {masked_payload}"
        )

        self._snapshot_fn()
        depth = self._chain_depth(event_payload)
        if instance is not None and not instance.get("fingerprint"):
            instance = {**instance, "fingerprint": config_fingerprint(instance.get("config"))}
        dispatches = []
        with self._rules_lock:
            snapshot = self._snapshot_fn()
            due, matches = snapshot.runtime.process_event(
                ((compiled.rule_id, compiled.source) for compiled in snapshot.event_index.get(event_type, ())),
                event_type, event_payload, instance=instance,
            )
            for key, events in due:
                dispatches.append((snapshot.compiled[key].source, key, events, "absence", {}, {}, 0))
            for key, events in matches:
                dispatches.append((snapshot.compiled[key].source, key, events, event_type, event_payload, masked_payload, depth))
            if dispatches and snapshot.generation is not None:
                snapshot.generation.retain()
        try:
            for rule, key, matched_events, kind, payload, masked, run_depth in dispatches:
                self._dispatch(snapshot, rule, key, kind, payload, masked, matched_events, run_depth)
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
            for key, matched in snapshot.runtime.take_due(now):
                dispatches.append((snapshot.compiled[key].source, key, matched))
            if dispatches and snapshot.generation is not None:
                snapshot.generation.retain()
        try:
            for rule, key, matched in dispatches:
                self._dispatch(snapshot, rule, key, "absence", {}, {}, matched)
        finally:
            if dispatches and snapshot.generation is not None:
                snapshot.generation.release()

    def _dispatch(self, snapshot, rule, key, event_type, event_payload, masked_payload, matched_events, depth=0):
        rule_id = rule.get("rule_id", "")
        rule_name = rule.get("name", "未命名规则")
        if depth > _MAX_RUN_CHAIN:
            self._drop_deep_chain(rule_id, rule_name, event_type, depth)
            return
        run_id = f"run_{uuid.uuid4().hex}"
        self._remember_run_depth(run_id, depth)
        print(f"[EventBus] [OK] 匹配到规则: <{rule_name}>, 准备分发动作！")
        self._safe_on_event(
            "run.triggered",
            {
                "rule_id": rule_id,
                "run_id": run_id,
                "rule_name": rule_name,
                "event_type": event_type,
                "action_count": len(rule.get("actions", [])),
                "event_payload": masked_payload,
            },
        )
        context = build_context(
            rule_name,
            event_type,
            event_payload,
            matched_events,
            rule_id,
            run_id,
        )
        try:
            snapshot.prepare_context(rule, context)
        except (DataTypeError, BindingResolutionError) as error:
            context["_preparation_error"] = error.as_dict()
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

    def _mask_event_payload(
        self, event_type: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """把触发器 outputs 声明 sensitive 的字段换成掩码再对外输出"""
        outputs = self._triggers_meta_fn().get(event_type, {}).get("outputs") or []
        sensitive_names = {
            output.get("name")
            for output in outputs
            if isinstance(output, dict) and output.get("sensitive") is True
        }
        if not sensitive_names or not isinstance(payload, dict):
            return payload
        masked = dict(payload)
        for name in sensitive_names:
            if name in masked:
                masked[name] = "***"
        return masked

    def _collect_sensitive_values(self, context: Dict[str, Any]) -> set:
        """收集上下文里声明 sensitive 的字段当前值，用于掩码动作参数"""
        generation = context.get("_generation")
        plugins = context.get("_plugins") or (generation.plugins if generation is not None else None)
        triggers_meta = plugins.triggers_meta if plugins is not None else self._triggers_meta_fn()
        actions_meta = plugins.actions_meta if plugins is not None else self._actions_meta_fn()
        values: set = set()

        def add_strings(value: Any) -> None:
            if isinstance(value, str):
                if value:
                    values.add(value)
            elif isinstance(value, dict):
                for item in value.values():
                    add_strings(item)
            elif isinstance(value, list):
                for item in value:
                    add_strings(item)

        def collect(meta: Dict[str, Any], payload: Any) -> None:
            if not isinstance(payload, dict):
                return
            for output in meta.get("outputs") or []:
                if not isinstance(output, dict) or output.get("sensitive") is not True:
                    continue
                value = payload.get(output.get("name"))
                add_strings(value)

        event = context.get("event") or {}
        collect(
            triggers_meta.get(event.get("type", ""), {}),
            event.get("payload"),
        )
        for trigger in (context.get("triggers") or {}).values():
            if isinstance(trigger, dict):
                collect(
                    triggers_meta.get(trigger.get("type", ""), {}),
                    trigger.get("payload"),
                )
        for step in (context.get("steps") or {}).values():
            if isinstance(step, dict):
                collect(
                    actions_meta.get(step.get("type", ""), {}),
                    step.get("result"),
                )
        for scope, bucket in (("constant", "constants"), ("variable", "variables")):
            marked = context.get("_sensitive_sources", {}).get(scope, {})
            for identity in marked:
                add_strings(context.get(bucket, {}).get(identity))
        for identity in context.get("_sensitive_sources", {}).get("step", {}):
            add_strings(context.get("steps", {}).get(identity, {}).get("result"))
        return values
