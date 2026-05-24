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

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if hasattr(module, "run"):
                func_store[plugin_id] = getattr(module, "run")
                print(f"[Engine] 装载{store_name}: {meta.get('name', plugin_id)}")

    def call_notmyfault(self, event_data: Dict[str, Any]) -> None:
        trigger_id = event_data.get("trigger_id")
        triggered_params = event_data.get("triggered_params", {})
        print(f"[Engine] 收到触发器事件: {trigger_id} -> {triggered_params}")

        for rule in self.rules:
            rule_trigger = rule.get("trigger", {})
            if rule_trigger.get("type") != trigger_id:
                continue

            expected_params = rule_trigger.get("params", {})
            is_match = all(triggered_params.get(key) == value for key, value in expected_params.items())
            if not is_match:
                continue

            print(f"[Engine] 规则匹配成功: {rule_trigger}")
            for action in rule.get("actions", []):
                self.execute_action(action)

    def execute_action(self, action: Dict[str, Any]) -> None:
        action_type = action.get("type")
        params = action.get("params", {})

        if action_type in self.actions_funcs:
            action_meta = self.actions_meta.get(action_type, {})
            action_func = self.actions_funcs[action_type]
            action_func(action_meta, params)
        else:
            print(f"[Engine] 未知 action 类型或未装载模块: {action_type}")

    def start(self) -> None:
        thread_count = 0
        for rule in self.rules:
            trigger_type = rule.get("trigger", {}).get("type")
            params = rule.get("trigger", {}).get("params", {})
            if trigger_type not in self.triggers_funcs:
                continue

            trigger_meta = self.triggers_meta.get(trigger_type, {})
            trigger_func = self.triggers_funcs[trigger_type]
            mode = trigger_meta.get("mode", "continuous")

            if mode == "continuous":
                thread_count += 1
                thread = threading.Thread(
                    target=trigger_func,
                    args=(trigger_meta, params, self.call_notmyfault),
                    daemon=True,
                )
                thread.start()
                print(f"[Engine] 已启动触发器线程: {trigger_type}")
            elif mode == "single":
                trigger_func(trigger_meta, params, self.call_notmyfault)
                print(f"[Engine] 已执行单次触发器: {trigger_type}")

        if thread_count == 0:
            print("[Engine] 没有找到可用触发器，程序将退出。")
            return

        try:
            while True:
                threading.Event().wait(1)
        except KeyboardInterrupt:
            print("[Engine] 主程序收到中断，退出中...")
