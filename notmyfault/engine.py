import importlib.util
import json
import os
import threading
from typing import Any, Dict, List


class AutomationEngine:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.rules: List[Dict[str, Any]] = config.get("rules", [])
        self.triggers_meta: Dict[str, Dict[str, Any]] = {}
        self.triggers_funcs: Dict[str, Any] = {}
        self.actions_meta: Dict[str, Dict[str, Any]] = {}
        self.actions_funcs: Dict[str, Any] = {}

    def auto_load(self, base_dir: str) -> None:
        self._load_plugins(
            base_dir=base_dir,
            plugins_dir="triggers",
            json_filename="trigger.json",
            py_filename="trigger.py",
            module_prefix="notmyfault.trigger_",
            meta_store=self.triggers_meta,
            func_store=self.triggers_funcs,
            store_name="触发器",
        )
        self._load_plugins(
            base_dir=base_dir,
            plugins_dir="actions",
            json_filename="action.json",
            py_filename="action.py",
            module_prefix="notmyfault.action_",
            meta_store=self.actions_meta,
            func_store=self.actions_funcs,
            store_name="执行器",
        )

    def _load_plugins(
        self,
        base_dir: str,
        plugins_dir: str,
        json_filename: str,
        py_filename: str,
        module_prefix: str,
        meta_store: Dict[str, Dict[str, Any]],
        func_store: Dict[str, Any],
        store_name: str,
    ) -> None:
        root_dir = os.path.join(base_dir, plugins_dir)
        if not os.path.isdir(root_dir):
            return

        for folder_name in os.listdir(root_dir):
            folder_path = os.path.join(root_dir, folder_name)
            if not os.path.isdir(folder_path):
                continue

            json_file = os.path.join(folder_path, json_filename)
            py_file = os.path.join(folder_path, py_filename)
            if not os.path.exists(json_file) or not os.path.exists(py_file):
                continue

            with open(json_file, "r", encoding="utf-8") as fp:
                meta = json.load(fp)

            plugin_id = meta.get("id")
            if not plugin_id:
                continue

            meta_store[plugin_id] = meta
            module_name = f"{module_prefix}{plugin_id}"
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                continue

            try:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as e:
                print(f"[Engine] 装载{store_name} 插件 {plugin_id} 失败: {e}")
                continue

            try:
                if hasattr(module, "run"):
                    func_store[plugin_id] = getattr(module, "run")
                    print(f"[Engine] 装载{store_name}: {meta.get('name', plugin_id)}")
            except Exception as e:
                print(f"[Engine] 装载{store_name} 插件 {plugin_id} 失败: {e}")

    def emit_event(self, event_type: str, event_payload: Dict[str, Any]) -> None:
        print(f"[EventBus] 收到广播事件: [{event_type}] -> {event_payload}")

        for rule in self.rules:
            rule_event = rule.get("event", {}) or rule.get("trigger", {})
            if rule_event.get("type") != event_type:
                continue

            expected_params = rule_event.get("params", {})
            if not self._match_event(expected_params, event_payload):
                continue

            print(f"[EventBus] 规则匹配成功: {rule_event}")
            for action in rule.get("actions", []):
                self.execute_action(action)

    def _match_event(self, expected: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        for key, expected_value in expected.items():
            actual_value = payload.get(key)
            if key == "process_name" and isinstance(expected_value, str) and isinstance(actual_value, str):
                if expected_value.lower() != actual_value.lower():
                    return False
            else:
                if expected_value != actual_value:
                    return False
        return True

    def call_notmyfault(self, event_data: Dict[str, Any]) -> None:
        event_type = event_data.get("trigger_id")
        event_payload = event_data.get("triggered_params", {})
        self.emit_event(event_type, event_payload)

    def execute_action(self, action: Dict[str, Any]) -> None:
        action_type = action.get("type")
        params = action.get("params", {})

        if action_type in self.actions_funcs:
            action_meta = self.actions_meta.get(action_type, {})
            action_func = self.actions_funcs[action_type]
            try:
                action_func(action_meta, params)
            except Exception as e:
                print(f"[Engine] 执行 action {action_type} 失败: {e}")
        else:
            print(f"[Engine] 未知 action 类型或未装载模块: {action_type}")

    def start(self) -> None:
        aggregated_event_configs: Dict[str, List[Dict[str, Any]]] = {}
        for rule in self.rules:
            event = rule.get("event", {}) or rule.get("trigger", {})
            event_type = event.get("type")
            event_params = event.get("params", {})
            if not event_type:
                continue

            aggregated_event_configs.setdefault(event_type, []).append(event_params)

        thread_count = 0
        for event_type, config_list in aggregated_event_configs.items():
            if event_type not in self.triggers_funcs:
                continue

            trigger_meta = self.triggers_meta.get(event_type, {})
            trigger_func = self.triggers_funcs[event_type]
            mode = trigger_meta.get("mode", "continuous")

            if mode == "continuous":
                thread_count += 1
                thread = threading.Thread(
                    target=trigger_func,
                    args=(trigger_meta, config_list, self.emit_event),
                    daemon=True,
                )
                thread.start()
                print(f"[Engine] 已启动触发器线程: {event_type} (共监听 {len(config_list)} 条规则)")
            elif mode == "single":
                trigger_func(trigger_meta, config_list, self.emit_event)
                print(f"[Engine] 已执行单次触发器: {event_type}")

        if thread_count == 0:
            print("[Engine] 没有找到可用触发器，程序将退出。")
            return

        try:
            while True:
                threading.Event().wait(1)
        except KeyboardInterrupt:
            print("[Engine] 主程序收到中断，退出中...")
