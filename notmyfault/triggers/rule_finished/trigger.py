"""监视指定规则的运行结束

通过 plugin_api.engine_events_api() 订阅引擎终态事件。
同一 run_id 只触发一次；默认不响应本规则自己的结束。
"""

from collections import deque

from notmyfault.plugin_api import engine_events_api

_OUTCOME_ALIASES = {
    "any": frozenset({"succeeded", "failed", "cancelled", "dropped", "replaced"}),
    "succeeded": frozenset({"succeeded"}),
    "failed": frozenset({"failed"}),
    "cancelled": frozenset({"cancelled"}),
    "dropped": frozenset({"dropped", "replaced"}),
}


def _status_from_event(event_type, data):
    if event_type == "workflow_completed":
        status = data.get("status", "succeeded")
        return status if isinstance(status, str) else "succeeded"
    if event_type == "workflow_failed":
        return "failed"
    if event_type == "run_dropped":
        return "dropped"
    if event_type == "run_replaced":
        return "replaced"
    return ""


class RuleFinishedTrigger:
    def __init__(self, meta, config, emit_event, shutdown_event):
        self.meta = meta
        self.config = config if isinstance(config, dict) else {}
        self._emit_event = emit_event
        self._stop_event = shutdown_event
        self.trigger_id = str(meta.get("id", "rule_finished"))
        self._token = None
        self._seen_runs = deque(maxlen=256)
        self._seen_run_set = set()
        self._api = engine_events_api()

    def log(self, message):
        print(f"[Trigger:{self.trigger_id}] {message}", flush=True)

    def validate(self):
        rule_id = str(self.config.get("rule_id", "") or "").strip()
        if not rule_id:
            raise ValueError("未选择要监视的规则")
        outcome = str(self.config.get("outcome", "any") or "any")
        if outcome not in _OUTCOME_ALIASES:
            raise ValueError(
                f"无效的结束状态: {outcome!r}（可选: {', '.join(sorted(_OUTCOME_ALIASES))}）"
            )
        self.watch_rule_id = rule_id
        self.outcome = outcome
        self.include_self = bool(self.config.get("include_self", False))
        self.owner_rule_id = str(self.config.get("owner_rule_id", "") or "").strip()

    def _should_emit(self, event_type, data):
        finished_rule_id = str(data.get("rule_id", "") or "").strip()
        if finished_rule_id != self.watch_rule_id:
            return False
        if (
            not self.include_self
            and self.owner_rule_id
            and finished_rule_id == self.owner_rule_id
        ):
            return False
        status = _status_from_event(event_type, data)
        if status not in _OUTCOME_ALIASES[self.outcome]:
            return False
        run_id = str(data.get("run_id", "") or "").strip()
        if run_id:
            if run_id in self._seen_run_set:
                return False
            if len(self._seen_runs) == self._seen_runs.maxlen:
                dropped = self._seen_runs[0]
                self._seen_run_set.discard(dropped)
            self._seen_runs.append(run_id)
            self._seen_run_set.add(run_id)
        return True

    def _on_engine_event(self, event_type, data):
        if self._stop_event.is_set():
            return
        if event_type not in self._api.RULE_TERMINAL_EVENTS:
            return
        if not isinstance(data, dict):
            return
        if not self._should_emit(event_type, data):
            return
        status = _status_from_event(event_type, data)
        payload = {
            "rule_id": str(data.get("rule_id", "") or ""),
            "rule_name": str(data.get("rule_name", "") or ""),
            "run_id": str(data.get("run_id", "") or ""),
            "status": status,
            "event_type": event_type,
        }
        self.log(
            f"规则 {payload['rule_name'] or payload['rule_id']} "
            f"结束（{status}），触发后续动作"
        )
        self._emit_event(payload)

    def run(self):
        self.validate()
        self._token = self._api.subscribe(self._on_engine_event)
        self.log(f"开始监视规则: {self.watch_rule_id}")
        try:
            self._stop_event.wait()
        finally:
            if self._token is not None:
                self._api.unsubscribe(self._token)
                self._token = None


def run(meta, config, emit_event, shutdown_event):
    RuleFinishedTrigger(meta, config, emit_event, shutdown_event).run()
