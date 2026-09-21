import copy
import tempfile
from pathlib import Path
from notmyfault.simulator.environment import SimulatedEnvironment


_SIM_DEMO_RULES = [
    {
        "name": "微信音量规则",
        "event": {
            "type": "process_state",
            "params": {"process_name": "WeChat.exe", "state": "running"},
        },
        "actions": [
            {"type": "set_volume", "params": {"action": "max"}},
            {"type": "notify", "params": {"title": "微信正在运行", "message": "音量已设置为100%"}},
        ],
    },
    {
        "name": "微信退出-恢复音量",
        "event": {
            "type": "process_state",
            "params": {"process_name": "WeChat.exe", "state": "stopped"},
        },
        "actions": [
            {"type": "set_volume", "params": {"action": "half"}},
            {"type": "notify", "params": {"title": "微信已退出", "message": "音量已恢复至50%"}},
        ],
    },
]


class _SimulationRulesStore:
    def __init__(self, rules):
        self._rules = rules
        self._directory = tempfile.TemporaryDirectory(prefix="notmyfault-simulator-")
        directory = Path(self._directory.name)
        self.rules_path = str(directory / "rules.json")
        self.plugin_manifest_path = str(directory / "plugin_manifest.json")

    def close(self):
        self._directory.cleanup()

    def load_verified_rules(self):
        return copy.deepcopy(self._rules)


class SimulatedRunner:
    def __init__(self, env):
        self.env = env
        self.events = []
        self.engine = None
        self._store = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.stop()

    def start(self, config=None):
        from notmyfault.core.engine import AutomationEngine
        from notmyfault.core.rules import get_rule_events, iter_action_nodes

        self.stop()
        if config is None:
            from notmyfault.config import DEFAULT_CONFIG
            config = copy.deepcopy(DEFAULT_CONFIG)
            # 默认配置不再携带示例规则，模拟器自带微信音量演示规则
            config["rules"] = copy.deepcopy(_SIM_DEMO_RULES)
        self._store = _SimulationRulesStore(config.get("rules", []))
        try:
            self.engine = AutomationEngine(config, on_event=self._on, rules_store=self._store)
        except Exception:
            self._store.close()
            self._store = None
            raise
        self.engine._alert_user = lambda *a, **kw: None
        self.engine._workflow_executor._action_resolver = None
        for rule in config.get("rules", []):
            for event in get_rule_events(rule):
                self.engine.triggers_meta[event["type"]] = {"semantic": "state"}
            for action, _path in iter_action_nodes(rule.get("actions", [])):
                action_type = action.get("type")
                if action_type not in ("if", "set_variable"):
                    self.engine.actions_funcs[action_type] = lambda meta, params: None
                    self.engine.actions_meta[action_type] = {}

    def _on(self, et, data):
        self.events.append({"type": et, "data": data})

    def emit(self, tid, payload=None):
        if self.engine:
            self.engine.emit_event(tid, payload or {})
            if not self.engine._rule_scheduler.wait_for_idle(timeout=5):
                raise TimeoutError("模拟动作未能在 5 秒内完成")

    def step(self, seconds=1.0):
        self.env.time.advance(seconds)

    def stop(self):
        if self.engine:
            self.engine.shutdown()
            if not self.engine._shutdown_clean:
                raise RuntimeError("模拟引擎尚未完全停止")
            self.engine = None
        if self._store is not None:
            self._store.close()
            self._store = None

    def last_action(self):
        for ev in reversed(self.events):
            if ev["type"] == "action_executed":
                return ev["data"]
        return None

    def assert_action(self, action_type, params=None):
        last = self.last_action()
        if last is None: return False
        if last.get("action_type") != action_type: return False
        if params is not None and last.get("params") != params: return False
        return True

    def clear_events(self):
        self.events.clear()
