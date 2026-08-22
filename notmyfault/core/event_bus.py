"""事件分发，从 engine.py 拆出。

收到触发器事件后：掩码 sensitive 字段、锁内取规则快照、锁外用
ConditionRuntime 逐条匹配，命中就发 rule_triggered 事件并交给
workflow 执行。call_notmyfault 是触发器线程推送外部事件的入口。
"""
import uuid
from typing import Any, Callable, Dict, List, Optional

from notmyfault.core.logging import engine_warn
from notmyfault.core.workflow import build_context


class EventBus:
    """收事件、匹配规则、分发动作"""

    def __init__(
        self,
        rules_fn: Callable[[], List[Dict[str, Any]]],
        rules_lock: Any,
        condition_runtime: Any,
        triggers_meta_fn: Callable[[], Dict[str, Dict[str, Any]]],
        actions_meta_fn: Callable[[], Dict[str, Dict[str, Any]]],
        is_shutdown_fn: Callable[[], bool],
        safe_on_event: Callable[[str, Dict[str, Any]], None],
        execute_workflow_cb: Callable[..., None],
        scheduler_submit_fn: Callable[..., str] | None = None,
    ) -> None:
        self._rules_fn = rules_fn
        # 和 engine 共用同一把锁对象，快照和热重载换规则才不会打架
        self._rules_lock = rules_lock
        self._condition_runtime = condition_runtime
        self._triggers_meta_fn = triggers_meta_fn
        self._actions_meta_fn = actions_meta_fn
        self._is_shutdown_fn = is_shutdown_fn
        self._safe_on_event = safe_on_event
        self._execute_workflow_cb = execute_workflow_cb
        # 有调度器时先决定跑不跑，没有就直接执行，旧测试路径走这里
        self._scheduler_submit_fn = scheduler_submit_fn

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

        with self._rules_lock:
            # 规则快照在锁内复制，动作执行在锁外进行
            rules_snapshot = list(self._rules_fn())

        for rule in rules_snapshot:
            rule_id = rule.get("rule_id", "")
            # 调度 key 用 rule_id，没有就用规则名；带数组下标会在规则重排后串到别的规则
            rule_key = rule_id or str(rule.get("name", ""))
            if not self._condition_runtime.match(
                rule_key,
                rule,
                event_type,
                event_payload,
                instance=instance,
            ):
                continue

            rule_name = rule.get("name", "未命名规则")
            run_id = f"run_{uuid.uuid4().hex}"
            print(f"[EventBus] [OK] 匹配到规则: <{rule_name}>, 准备分发动作！")
            self._safe_on_event(
                "rule_triggered",
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
                self._condition_runtime.last_match(rule_key),
                rule_id,
                run_id,
            )
            if self._scheduler_submit_fn is not None:
                self._scheduler_submit_fn(rule_key, rule, rule_name, context)
            else:
                self._execute_workflow_cb(rule_key, rule, rule_name, context)

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
        triggers_meta = self._triggers_meta_fn()
        actions_meta = self._actions_meta_fn()
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
        return values
