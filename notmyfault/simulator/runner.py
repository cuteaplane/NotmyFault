import copy
import tempfile
from pathlib import Path
from notmyfault.simulator.environment import SimulatedEnvironment


_SIM_DEMO_RULES = [
    {
        "rule_id": f"r_wechat_{state}",
        "name": name,
        "workflow": {
            "nodes": [
                {
                    "type": "trigger",
                    "binding_id": f"a_wechat_{state}",
                    "trigger": {
                        "type": "process_state",
                        "binding_id": f"t_wechat_{state}",
                        "params": {"process_name": "WeChat.exe", "state": state},
                    },
                },
                {"type": "set_volume", "binding_id": f"a_volume_{state}", "params": {"action": volume}},
                {"type": "notify", "binding_id": f"a_notify_{state}", "params": {"title": title, "message": message}},
            ],
            "edges": [
                {"source": f"a_wechat_{state}", "port": "event", "target": f"a_volume_{state}", "channel": "control"},
                {"source": f"a_volume_{state}", "port": "next", "target": f"a_notify_{state}", "channel": "control"},
            ],
        },
    }
    for state, name, volume, title, message in (
        ("running", "微信音量规则", "max", "微信正在运行", "音量已设置为100%"),
        ("stopped", "微信退出-恢复音量", "half", "微信已退出", "音量已恢复至50%"),
    )
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
        from notmyfault.core.workflow_graph import get_all_rule_events, iter_rule_actions, NATIVE_NODES

        self.stop()
        if config is None:
            from notmyfault.config import DEFAULT_CONFIG
            config = copy.deepcopy(DEFAULT_CONFIG)
            # 默认配置没有示例规则，模拟器自带微信音量演示规则
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
            for event in get_all_rule_events(rule):
                self.engine.triggers_meta[event["type"]] = {"semantic": "state"}
            for action, _path in iter_rule_actions(rule):
                action_type = action.get("type")
                if action_type not in NATIVE_NODES:
                    self.engine.actions_funcs[action_type] = lambda meta, params, context: None
                    self.engine.actions_meta[action_type] = {}

    def _on(self, event):
        self.events.append(event.to_dict())

    def emit(self, tid, payload=None):
        if self.engine:
            self.engine.emit_event(tid, payload or {})
            if not self.engine._run_manager.wait_for_idle(timeout=5):
                raise TimeoutError("模拟动作未能在 5 秒内完成")
            if not self.engine.events.flush():
                raise TimeoutError("模拟事件未能在 5 秒内处理完毕")

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
            if ev["type"] == "run.step.succeeded":
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
