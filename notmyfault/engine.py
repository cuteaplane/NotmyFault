import importlib.util
import json
import os
import threading
from typing import Any, Callable, Dict, List, Optional


def _normalize_process_name(name: str) -> str:
    """标准化进程名：转小写，补全 .exe 后缀"""
    n = (name or "").strip().lower()
    if n and not n.endswith(".exe"):
        n += ".exe"
    return n


class AutomationEngine:
    def __init__(self, config: Dict[str, Any],
                 on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> None:
        self.config = config
        self.rules: List[Dict[str, Any]] = config.get("rules", [])
        self.on_event = on_event  # 事件回调: (event_type, data_dict)
        self.triggers_meta: Dict[str, Dict[str, Any]] = {}
        self.triggers_funcs: Dict[str, Any] = {}
        self.actions_meta: Dict[str, Dict[str, Any]] = {}
        self.actions_funcs: Dict[str, Any] = {}
        # 优雅关闭
        self._active_actions = 0
        self._action_lock = threading.Lock()
        self._action_done = threading.Condition()
        self._shutdown_flag: "threading.Event | None" = None

    def auto_load(self, base_dir: str) -> None:
        self._load_plugins(
            base_dir=base_dir,
            plugins_dir="triggers",
            json_filename="trigger.json",
            py_filename="trigger.py",
            module_prefix="notmyfault.trigger_",
            meta_store=self.triggers_meta,
            func_store=self.triggers_funcs,
            store_name="Trigger",
        )
        self._load_plugins(
            base_dir=base_dir,
            plugins_dir="actions",
            json_filename="action.json",
            py_filename="action.py",
            module_prefix="notmyfault.action_",
            meta_store=self.actions_meta,
            func_store=self.actions_funcs,
            store_name="Actioner",
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
        """
        分发事件给所有规则。
        event_type: 事件类型ID（触发器 ID）
        event_payload: 事件参数
        semantic 由触发器元数据定义（state=持续状态上报, oneshot=单次触发）
        """
        # 关闭期间拒绝新事件，但允许完成已在队列中的事件
        if self._shutdown_flag and self._shutdown_flag.is_set():
            print(f"[EventBus] 引擎正在关闭，忽略事件: [{event_type}]")
            return

        semantic = self.triggers_meta.get(event_type, {}).get("semantic", "oneshot")
        print(f"[EventBus] 收到广播事件: [{event_type}] ({semantic}) -> {event_payload}")

        for rule in self.rules:
            rule_event = rule.get("event", {}) or rule.get("trigger", {})
            if rule_event.get("type") != event_type:
                continue

            expected_params = rule_event.get("params", {})
            is_match = True

            for key, expected_val in expected_params.items():
                actual_val = event_payload.get(key)
                # 进程名标准化比较：大小写不敏感，统一补全 .exe
                if key == "process_name":
                    expected_val = _normalize_process_name(expected_val)
                    actual_val = _normalize_process_name(actual_val or "")
                if expected_val != actual_val:
                    is_match = False
                    break

            if is_match:
                rule_name = rule.get('name', '未命名规则')
                print(f"[EventBus] [OK] 匹配到规则: <{rule_name}>, 准备分发动作！")
                if self.on_event:
                    self.on_event("rule_triggered", {
                        "rule_name": rule_name,
                        "event_type": event_type,
                        "event_payload": event_payload,
                    })
                for action in rule.get("actions", []):
                    self.execute_action(action, rule_name=rule_name)

    def call_notmyfault(self, event_data: Dict[str, Any]) -> None:
        event_type = event_data.get("trigger_id")
        event_payload = event_data.get("triggered_params", {})
        self.emit_event(event_type, event_payload)

    def execute_action(self, action: Dict[str, Any], rule_name: str = "") -> None:
        # 关闭期间拒绝新动作
        if self._shutdown_flag and self._shutdown_flag.is_set():
            print(f"[Engine] 正在关闭，跳过动作: {action.get('type', '?')}")
            return

        action_type = action.get("type")
        params = action.get("params", {})

        if action_type not in self.actions_funcs:
            print(f"[Engine] [?] 未知 action 类型或未装载模块: {action_type}")
            return

        with self._action_lock:
            self._active_actions += 1
        try:
            action_meta = self.actions_meta.get(action_type, {})
            action_func = self.actions_funcs[action_type]
            action_func(action_meta, params)
            if self.on_event:
                self.on_event("action_executed", {
                    "action_type": action_type,
                    "params": params,
                    "rule_name": rule_name,
                    "status": "ok",
                })
        except Exception as e:
            print(f"[Engine] [ERR] 执行 action {action_type} 失败: {e}")
            if self.on_event:
                self.on_event("error", {
                    "action_type": action_type,
                    "rule_name": rule_name,
                    "error": str(e),
                })
        finally:
            with self._action_lock:
                self._active_actions -= 1
            with self._action_done:
                self._action_done.notify_all()

    def start(self, shutdown_event: "threading.Event | None" = None) -> None:
        self._shutdown_flag = shutdown_event or threading.Event()
        aggregated_event_configs: Dict[str, List[Dict[str, Any]]] = {}
        from Win_toaster.show_notification import show_notification
        from Win_toaster.AUMID_Register import register_toaster
        register_toaster()
        show_notification("NotmyFault 已加载", "")
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

            thread_count += 1
            thread = threading.Thread(
                target=trigger_func,
                args=(trigger_meta, config_list, self.emit_event),
                daemon=True,
            )
            thread.start()
            print(f"[Engine] 已启动触发器线程: {event_type} (共监听 {len(config_list)} 条规则)")

        if thread_count == 0:
            print("[Engine] 没有找到可用触发器，程序将退出。")
            return

        # 使用 shutdown_event 实现优雅关闭
        se = self._shutdown_flag

        try:
            while not se.is_set():
                se.wait(1)
        except KeyboardInterrupt:
            print("[Engine] 主程序收到中断，退出中...")

        # 等待在手活跃动作完成
        print("[Engine] 正在关闭，等待活跃动作完成...")
        while True:
            with self._action_lock:
                remaining = self._active_actions
            if remaining == 0:
                break
            print(f"[Engine] 等待 {remaining} 个活跃动作完成...")
            with self._action_done:
                self._action_done.wait(timeout=3)

        print("[Engine] 所有动作已完成，引擎安全关闭")

