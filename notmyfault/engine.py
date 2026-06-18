import ast
import importlib.util
import json
import os
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from notmyfault.config import CONFIG_FILE
from notmyfault.logging import engine_info, engine_warn, engine_error


# ---------------------------------------------------------------------------
# Plugin metadata schema
# ---------------------------------------------------------------------------

_REQUIRED_META_FIELDS = {"id", "name", "description", "enabled", "version_code"}
_TRIGGER_OPTIONAL_FIELDS = {"semantic", "params", "permissions"}
_ACTION_OPTIONAL_FIELDS = {"params", "permissions"}
_ALLOWED_SEMANTICS = {"state", "oneshot"}
_ALLOWED_PARAM_TYPES = {"string", "number", "select", "bool"}
_REQUIRED_PARAM_FIELDS = {"name", "type", "label"}
_ALLOWED_PERMISSIONS = {"admin"}


def _validate_plugin_meta(
    meta: Dict[str, Any], plugin_type: str
) -> Tuple[bool, List[str]]:
    """校验插件元数据 schema，返回 (is_valid, 错误列表)。"""
    errors: List[str] = []

    if not isinstance(meta, dict):
        return False, ["插件元数据不是有效的 JSON 对象"]

    # --- 必填字段 ---
    for field in sorted(_REQUIRED_META_FIELDS):
        if field not in meta:
            errors.append(f"缺少必填字段: {field}")

    # --- 字段类型校验 ---
    if "id" in meta and not isinstance(meta["id"], str):
        errors.append(f"字段 'id' 必须是字符串，实际: {type(meta['id']).__name__}")
    if "name" in meta and not isinstance(meta["name"], str):
        errors.append(f"字段 'name' 必须是字符串")
    if "description" in meta and not isinstance(meta["description"], str):
        errors.append(f"字段 'description' 必须是字符串")
    if "enabled" in meta and not isinstance(meta["enabled"], bool):
        errors.append(f"字段 'enabled' 必须为布尔值 (true/false)，实际: {type(meta['enabled']).__name__}")
    if "version_code" in meta and not isinstance(meta["version_code"], int):
        errors.append(f"字段 'version_code' 必须为整数，实际: {type(meta['version_code']).__name__}")

    # --- semantic (仅触发器) ---
    if "semantic" in meta:
        if meta["semantic"] not in _ALLOWED_SEMANTICS:
            errors.append(
                f"字段 'semantic' 无效: '{meta['semantic']}'"
                f"（允许: {', '.join(sorted(_ALLOWED_SEMANTICS))}）"
            )

    # --- permissions ---
    if "permissions" in meta:
        perms = meta["permissions"]
        if not isinstance(perms, list):
            errors.append(f"字段 'permissions' 必须是数组")
        else:
            for perm in perms:
                if not isinstance(perm, str):
                    errors.append(f"permissions 中的值必须是字符串，实际: {type(perm).__name__}")
                elif perm not in _ALLOWED_PERMISSIONS:
                    errors.append(
                        f"未知权限类型: '{perm}'（目前仅支持: {', '.join(sorted(_ALLOWED_PERMISSIONS))}）"
                    )

    # --- params ---
    if "params" in meta:
        params = meta["params"]
        if not isinstance(params, list):
            errors.append(f"字段 'params' 必须是数组")
        else:
            for i, param in enumerate(params):
                if not isinstance(param, dict):
                    errors.append(f"params[{i}] 必须是对象")
                    continue
                for field in sorted(_REQUIRED_PARAM_FIELDS):
                    if field not in param:
                        errors.append(f"params[{i}] 缺少必填字段: {field}")
                ptype = param.get("type", "")
                if ptype and ptype not in _ALLOWED_PARAM_TYPES:
                    errors.append(
                        f"params[{i}].type 无效: '{ptype}'"
                        f"（允许: {', '.join(sorted(_ALLOWED_PARAM_TYPES))}）"
                    )
                if ptype == "select" and "options" not in param:
                    errors.append(f"params[{i}] (type=select) 必须提供 'options' 字段")

    # --- 未知字段 ---
    allowed_fields = (
        _REQUIRED_META_FIELDS | _TRIGGER_OPTIONAL_FIELDS
        if plugin_type == "trigger"
        else _REQUIRED_META_FIELDS | _ACTION_OPTIONAL_FIELDS
    )
    for key in meta:
        if key not in allowed_fields:
            errors.append(f"包含未知字段: '{key}'")

    return len(errors) == 0, errors


def _check_sudo_import(py_file_path: str) -> bool:
    """扫描 .py 源码是否 import 了 notmyfault.sudo（AST 级别检查）。"""
    try:
        with open(py_file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "notmyfault.sudo":
                        return True
            elif isinstance(node, ast.ImportFrom):
                if node.module == "notmyfault.sudo":
                    return True
                if node.module == "notmyfault":
                    for alias in node.names:
                        if alias.name == "sudo":
                            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# AutomationEngine
# ---------------------------------------------------------------------------


class AutomationEngine:
    """规则引擎：加载插件 → 匹配规则 → 执行动作。"""

    def __init__(
        self,
        config: Dict[str, Any],
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.config = config
        self.rules: List[Dict[str, Any]] = config.get("rules", [])
        self.on_event = on_event

        self.triggers_meta: Dict[str, Dict[str, Any]] = {}
        self.triggers_funcs: Dict[str, Any] = {}
        self.actions_meta: Dict[str, Dict[str, Any]] = {}
        self.actions_funcs: Dict[str, Any] = {}
        self._plugin_modules: Dict[str, Any] = {}  # plugin_id → module (用于 teardown)

        # 优雅关闭
        self._active_actions = 0
        self._action_lock = threading.Lock()
        self._action_done = threading.Condition()
        self._shutdown_flag: "threading.Event | None" = None

        # 规则热重载线程安全
        self._rules_lock = threading.RLock()

        # 诊断数据 (供 Dashboard 展示)
        self._start_time: float = 0.0
        self._diag: Dict[str, Any] = {
            "plugin_errors": [],      # [(plugin_id, reason), ...]
            "rule_issues": [],        # [(rule_name, issue), ...]
            "action_ok": 0,
            "action_fail": 0,
            "hot_reload_errors": 0,
        }

    # ------------------------------------------------------------------
    # 告警辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _alert_user(title: str, message: str, open_dashboard: bool = False) -> None:
        """向用户发送告警（包装 alert_user，静默忽略导入/发送失败）。"""
        try:
            from notmyfault.alert import alert_user
            alert_user(title, message, open_dashboard=open_dashboard)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def get_diagnostics(self) -> Dict[str, Any]:
        """返回引擎当前诊断快照（供 Dashboard 展示）。"""
        uptime = time.time() - self._start_time if self._start_time > 0 else 0
        total_actions = self._diag["action_ok"] + self._diag["action_fail"]
        return {
            "uptime_seconds": round(uptime, 1),
            "plugins": {
                "actions_loaded": len(self.actions_funcs),
                "triggers_loaded": len(self.triggers_funcs),
                "errors": self._diag["plugin_errors"][-20:],  # 最近 20 条
                "error_count": len(self._diag["plugin_errors"]),
            },
            "rules": {
                "total": len(self.rules),
                "issues": self._diag["rule_issues"],
                "issue_count": len(self._diag["rule_issues"]),
            },
            "actions": {
                "ok": self._diag["action_ok"],
                "fail": self._diag["action_fail"],
                "total": total_actions,
            },
            "hot_reload_errors": self._diag["hot_reload_errors"],
        }

    # ------------------------------------------------------------------
    # 插件加载
    # ------------------------------------------------------------------

    def auto_load(self, base_dir: str) -> None:
        """扫描并加载所有动作插件和触发器插件。"""
        engine_info("=== SESSION_START ===")
        t_loaded, t_failed = self._load_plugins(
            base_dir=base_dir,
            plugins_dir="triggers",
            json_filename="trigger.json",
            py_filename="trigger.py",
            module_prefix="notmyfault.trigger_",
            meta_store=self.triggers_meta,
            func_store=self.triggers_funcs,
            store_name="Trigger",
        )
        a_loaded, a_failed = self._load_plugins(
            base_dir=base_dir,
            plugins_dir="actions",
            json_filename="action.json",
            py_filename="action.py",
            module_prefix="notmyfault.action_",
            meta_store=self.actions_meta,
            func_store=self.actions_funcs,
            store_name="Actioner",
        )

        # 启动摘要
        print(
            f"\n[Engine] 已加载 {a_loaded} 个动作插件, {t_loaded} 个触发器插件"
        )

        # 插件加载失败 → 告警
        total_failed = t_failed + a_failed
        if total_failed > 0:
            parts = []
            if t_failed > 0:
                parts.append(f"{t_failed} 个触发器")
            if a_failed > 0:
                parts.append(f"{a_failed} 个动作")
            fail_msg = "、".join(parts) + " 插件加载失败，请检查引擎日志"
            print(f"[Engine] [!!] {fail_msg}", file=sys.stderr)
            self._alert_user("插件加载异常", fail_msg)

        # admin 权限提示
        admin_plugins = [
            pid
            for pid, meta in {**self.triggers_meta, **self.actions_meta}.items()
            if "admin" in (meta.get("permissions") or [])
        ]
        if admin_plugins:
            print(
                f"[Engine] [!!] 以下插件声明了 admin 权限: {', '.join(admin_plugins)}"
            )
        print()

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
    ) -> Tuple[int, int]:
        """通用插件加载器（触发器和动作共用）。

        Returns:
            (loaded_count, failed_count) — loaded 是成功加载数，failed 是出错数。
            故意跳过的（如 disabled、缺少文件）不计入 failed。
        """
        plugin_type = "trigger" if store_name == "Trigger" else "action"
        root_dir = os.path.join(base_dir, plugins_dir)
        loaded_count = 0
        failed_count = 0

        if not os.path.isdir(root_dir):
            print(f"[Engine] 插件目录不存在，跳过: {root_dir}", file=sys.stderr)
            return 0, 0

        for folder_name in sorted(os.listdir(root_dir)):
            folder_path = os.path.join(root_dir, folder_name)
            if not os.path.isdir(folder_path):
                continue

            json_file = os.path.join(folder_path, json_filename)
            py_file = os.path.join(folder_path, py_filename)

            # --- 文件存在检查（非错误：可能不是插件目录）---
            if not os.path.exists(json_file):
                print(
                    f"[Engine] 插件目录缺少 {json_filename}，跳过: {folder_path}",
                    file=sys.stderr,
                )
                continue
            if not os.path.exists(py_file):
                print(
                    f"[Engine] 插件目录缺少 {py_filename}，跳过: {folder_path}",
                    file=sys.stderr,
                )
                continue

            # --- JSON 解析 ---
            try:
                with open(json_file, "r", encoding="utf-8") as fp:
                    meta = json.load(fp)
            except json.JSONDecodeError as e:
                print(
                    f"[Engine] 插件 JSON 解析失败 ({json_file}): {e}",
                    file=sys.stderr,
                )
                failed_count += 1
                self._diag["plugin_errors"].append(
                    (store_name, folder_name, f"JSON 解析失败: {e}")
                )
                engine_error("plugin_load_failed", plugin=folder_name, type=store_name, reason=f"JSON 解析失败: {e}")
                continue
            except OSError as e:
                print(
                    f"[Engine] 无法读取插件元数据 ({json_file}): {e}",
                    file=sys.stderr,
                )
                failed_count += 1
                self._diag["plugin_errors"].append(
                    (store_name, folder_name, f"读取文件失败: {e}")
                )
                engine_error("plugin_load_failed", plugin=folder_name, type=store_name, reason=f"读取文件失败: {e}")
                continue

            # --- Schema 校验 ---
            is_valid, errors = _validate_plugin_meta(meta, plugin_type)
            plugin_id = meta.get("id", folder_name)
            if not is_valid:
                print(
                    f"[Engine] 插件 \"{plugin_id}\" schema 校验失败 ({json_file}):",
                    file=sys.stderr,
                )
                for err in errors:
                    print(f"         - {err}", file=sys.stderr)
                failed_count += 1
                self._diag["plugin_errors"].append(
                    (store_name, plugin_id, f"schema 校验失败: {'; '.join(errors[:3])}")
                )
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason=f"schema 校验失败: {'; '.join(errors[:3])}")
                continue
            if not meta["enabled"]:
                print(
                    f"[Engine] 插件 \"{plugin_id}\" ({meta['name']}) 已禁用，跳过"
                )
                continue

            # --- Python 模块加载 ---
            module_name = f"{module_prefix}{plugin_id}"
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                print(
                    f"[Engine] 无法创建模块规格，跳过: {py_file}",
                    file=sys.stderr,
                )
                failed_count += 1
                self._diag["plugin_errors"].append(
                    (store_name, plugin_id, "无法创建模块规格")
                )
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason="无法创建模块规格")
                continue

            try:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                print(
                    f"[Engine] 插件 \"{plugin_id}\" Python 加载失败 ({py_file}):",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                failed_count += 1
                self._diag["plugin_errors"].append(
                    (store_name, plugin_id, f"Python 加载异常: {traceback.format_exc()[-200:]}")
                )
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason="Python 加载异常")
                continue

            # --- 权限一致性检查 ---
            if _check_sudo_import(py_file):
                declared_perms = meta.get("permissions") or []
                if "admin" not in declared_perms:
                    print(
                        f"[Engine] [!!] 插件 \"{plugin_id}\" import 了 notmyfault.sudo "
                        f"但未在元数据中声明 'admin' 权限",
                        file=sys.stderr,
                    )

            # --- 提取 run 入口 ---
            if not hasattr(module, "run"):
                print(
                    f"[Engine] 插件 \"{plugin_id}\" ({meta['name']}) 缺少 run() 函数，跳过",
                    file=sys.stderr,
                )
                failed_count += 1
                self._diag["plugin_errors"].append(
                    (store_name, plugin_id, "缺少 run() 函数")
                )
                engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason="缺少 run() 函数")
                continue

            func_store[plugin_id] = getattr(module, "run")
            meta_store[plugin_id] = meta
            self._plugin_modules[plugin_id] = module
            loaded_count += 1

            # --- 生命周期: setup (触发器) ---
            if plugin_type == "trigger" and hasattr(module, "setup"):
                try:
                    result = module.setup(meta)
                    if result is False:
                        print(
                            f"[Engine] 触发器 \"{plugin_id}\" setup() 返回 False，"
                            f"卸载该插件",
                            file=sys.stderr,
                        )
                        del func_store[plugin_id]
                        del meta_store[plugin_id]
                        self._plugin_modules.pop(plugin_id, None)
                        loaded_count -= 1
                        failed_count += 1
                        self._diag["plugin_errors"].append(
                            (store_name, plugin_id, "setup() 返回 False")
                        )
                        engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason="setup() 返回 False")
                        continue
                except Exception:
                    print(
                        f"[Engine] 触发器 \"{plugin_id}\" setup() 执行异常:",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                    del func_store[plugin_id]
                    del meta_store[plugin_id]
                    self._plugin_modules.pop(plugin_id, None)
                    loaded_count -= 1
                    failed_count += 1
                    self._diag["plugin_errors"].append(
                        (store_name, plugin_id, "setup() 执行异常")
                    )
                    engine_error("plugin_load_failed", plugin=plugin_id, type=store_name, reason="setup() 执行异常")
                    continue

            version_info = (
                f" v{meta['version_code']}"
                if meta.get("version_code") is not None
                else ""
            )
            perm_info = ""
            if meta.get("permissions"):
                perm_info = f" [权限: {', '.join(meta['permissions'])}]"

            print(
                f"[Engine] 装载{store_name}: {meta['name']} ({plugin_id}){version_info}{perm_info}"
            )

        return loaded_count, failed_count

    # ------------------------------------------------------------------
    # 规则 & 参数校验
    # ------------------------------------------------------------------

    def _validate_all_rules(self) -> Tuple[int, int]:
        """校验所有规则的 event/action 引用和参数是否与已加载插件匹配。

        非致命：只打印警告，不拒绝任何规则。
        Returns:
            (valid_count, total_count)
        """
        with self._rules_lock:
            rules = list(self.rules)

        self._diag["rule_issues"] = []
        valid_count = 0
        for i, rule in enumerate(rules):
            rule_name = rule.get("name", f"规则 #{i+1}")
            event = rule.get("event", {}) or rule.get("trigger", {})
            event_type = event.get("type", "")

            # 检查 event 引用的触发器是否存在
            if event_type and event_type not in self.triggers_meta:
                issue = f"引用了未加载的触发器: {event_type}"
                print(
                    f"[Engine] [!!] 规则 \"{rule_name}\" {issue}",
                    file=sys.stderr,
                )
                self._diag["rule_issues"].append((rule_name, issue))
                engine_error("rule_issue", rule=rule_name, issue=issue)
                continue

            rule_ok = True
            for j, action in enumerate(rule.get("actions", [])):
                action_type = action.get("type", "")
                if not action_type:
                    issue = f"actions[{j}] 缺少 type"
                    print(
                        f"[Engine] [!!] 规则 \"{rule_name}\" {issue}",
                        file=sys.stderr,
                    )
                    self._diag["rule_issues"].append((rule_name, issue))
                    engine_error("rule_issue", rule=rule_name, issue=issue)
                    rule_ok = False
                    continue

                # 检查 action 引用的插件是否存在
                if action_type not in self.actions_meta:
                    issue = f"引用了未加载的 action: {action_type}"
                    print(
                        f"[Engine] [!!] 规则 \"{rule_name}\" {issue}",
                        file=sys.stderr,
                    )
                    self._diag["rule_issues"].append((rule_name, issue))
                    engine_error("rule_issue", rule=rule_name, issue=issue)
                    rule_ok = False
                    continue

                # 参数校验
                action_meta = self.actions_meta[action_type]
                schema_params = action_meta.get("params", [])
                schema_param_names = {p["name"]: p for p in schema_params}
                rule_params = action.get("params", {})

                for param_name, param_value in rule_params.items():
                    if param_name not in schema_param_names:
                        hint = ""
                        if schema_param_names:
                            hint = f"（可用参数: {', '.join(sorted(schema_param_names))}）"
                        print(
                            f"[Engine] [!!] 规则 \"{rule_name}\" action \"{action_type}\" "
                            f"使用了未知参数: '{param_name}' {hint}",
                            file=sys.stderr,
                        )
                        continue

                    schema = schema_param_names[param_name]
                    expected_type = schema.get("type", "string")

                    if expected_type == "number":
                        if not isinstance(param_value, (int, float)):
                            print(
                                f"[Engine] [!!] 规则 \"{rule_name}\" action \"{action_type}\" "
                                f"参数 '{param_name}' 应为数字，实际: {type(param_value).__name__}",
                                file=sys.stderr,
                            )
                    elif expected_type == "bool":
                        if not isinstance(param_value, bool):
                            print(
                                f"[Engine] [!!] 规则 \"{rule_name}\" action \"{action_type}\" "
                                f"参数 '{param_name}' 应为布尔值，实际: {type(param_value).__name__}",
                                file=sys.stderr,
                            )
                    elif expected_type == "select":
                        options = schema.get("options", [])
                        if options and param_value not in options:
                            print(
                                f"[Engine] [!!] 规则 \"{rule_name}\" action \"{action_type}\" "
                                f"参数 '{param_name}' 值 '{param_value}' 不在可选项中 "
                                f"({', '.join(map(str, options))})",
                                file=sys.stderr,
                            )
                    # string 类型不做严格检查

            if rule_ok:
                valid_count += 1

        print(
            f"[Engine] 规则校验完成: {valid_count}/{len(rules)} 条有效规则"
        )

        # 规则校验有问题 → 告警（仅通知，不自动弹 UI）
        if valid_count < len(rules):
            problem_count = len(rules) - valid_count
            self._alert_user(
                "规则配置异常",
                f"{problem_count} 条规则引用了未加载的插件或参数不匹配，请检查引擎日志",
            )

        return valid_count, len(rules)

    # ------------------------------------------------------------------
    # 事件分发
    # ------------------------------------------------------------------

    def emit_event(self, event_type: str, event_payload: Dict[str, Any]) -> None:
        """分发事件给匹配的规则。"""
        if self._shutdown_flag and self._shutdown_flag.is_set():
            print(f"[EventBus] 引擎正在关闭，忽略事件: [{event_type}]")
            return

        semantic = self.triggers_meta.get(event_type, {}).get("semantic", "oneshot")
        print(
            f"[EventBus] 收到广播事件: [{event_type}] ({semantic}) → {event_payload}"
        )

        with self._rules_lock:
            rules_snapshot = list(self.rules)

        for rule in rules_snapshot:
            rule_event = rule.get("event", {}) or rule.get("trigger", {})
            if rule_event.get("type") != event_type:
                continue

            expected_params = rule_event.get("params", {})
            is_match = True
            for key, expected_val in expected_params.items():
                actual_val = event_payload.get(key)
                if expected_val != actual_val:
                    is_match = False
                    break

            if is_match:
                rule_name = rule.get("name", "未命名规则")
                print(f"[EventBus] [OK] 匹配到规则: <{rule_name}>, 准备分发动作！")
                if self.on_event:
                    self.on_event(
                        "rule_triggered",
                        {
                            "rule_name": rule_name,
                            "event_type": event_type,
                            "event_payload": event_payload,
                        },
                    )
                for action in rule.get("actions", []):
                    self.execute_action(action, rule_name=rule_name)

    def call_notmyfault(self, event_data: Dict[str, Any]) -> None:
        """接收外部事件（触发器线程通过此方法推送事件）。"""
        event_type = event_data.get("trigger_id")
        event_payload = event_data.get("triggered_params", {})
        self.emit_event(event_type, event_payload)

    # ------------------------------------------------------------------
    # 动作执行
    # ------------------------------------------------------------------

    def execute_action(self, action: Dict[str, Any], rule_name: str = "") -> None:
        """执行单个动作。"""
        if self._shutdown_flag and self._shutdown_flag.is_set():
            print(f"[Engine] 正在关闭，跳过动作: {action.get('type', '?')}")
            return

        action_type = action.get("type")
        params = action.get("params", {})

        if action_type not in self.actions_funcs:
            print(
                f"[Engine] [?] 未知 action 类型或未装载模块: {action_type}",
                file=sys.stderr,
            )
            return

        # --- 插件自定义参数校验 ---
        module = self._plugin_modules.get(action_type)
        if module is not None and hasattr(module, "validate_params"):
            try:
                validation_errors = module.validate_params(
                    self.actions_meta.get(action_type, {}), params
                )
                if validation_errors:
                    print(
                        f"[Engine] [!!] action \"{action_type}\" validate_params 警告:",
                        file=sys.stderr,
                    )
                    for ve in validation_errors:
                        print(f"         - {ve}", file=sys.stderr)
            except Exception:
                print(
                    f"[Engine] action \"{action_type}\" validate_params() 执行异常:",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)

        with self._action_lock:
            self._active_actions += 1

        try:
            action_meta = self.actions_meta.get(action_type, {})
            action_func = self.actions_funcs[action_type]
            action_func(action_meta, params)
            self._diag["action_ok"] += 1
            if self.on_event:
                self.on_event(
                    "action_executed",
                    {
                        "action_type": action_type,
                        "params": params,
                        "rule_name": rule_name,
                        "status": "ok",
                    },
                )
        except Exception:
            self._diag["action_fail"] += 1
            err_msg = traceback.format_exc()
            print(
                f"[Engine] [ERR] 执行 action \"{action_type}\" 失败:",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            engine_error("action_failed", action_type=action_type, rule_name=rule_name, error=str(err_msg[-500:]))
            if self.on_event:
                self.on_event(
                    "error",
                    {
                        "action_type": action_type,
                        "rule_name": rule_name,
                        "error": traceback.format_exc(),
                    },
                )
        finally:
            with self._action_lock:
                self._active_actions -= 1
            with self._action_done:
                self._action_done.notify_all()

    # ------------------------------------------------------------------
    # 引擎生命周期
    # ------------------------------------------------------------------

    def start(
        self, shutdown_event: "threading.Event | None" = None
    ) -> None:
        """启动引擎：加载规则、启动触发器线程、进入主循环。"""
        self._start_time = time.time()
        self._shutdown_flag = shutdown_event or threading.Event()

        # 校验规则（初始加载）
        self._validate_all_rules()

        # 通知系统就绪
        from Win_toaster.show_notification import show_notification
        from Win_toaster.AUMID_Register import register_toaster

        register_toaster()
        show_notification("NotmyFault 已加载", "")

        # 聚合规则中的触发器配置
        aggregated_event_configs: Dict[str, List[Dict[str, Any]]] = {}
        with self._rules_lock:
            rules_snapshot = list(self.rules)

        for rule in rules_snapshot:
            event = rule.get("event", {}) or rule.get("trigger", {})
            event_type = event.get("type")
            event_params = event.get("params", {})
            if not event_type:
                continue
            aggregated_event_configs.setdefault(event_type, []).append(event_params)

        # 检查规则引用了但未加载的触发器（在启动线程之前告警）
        missing_triggers = [
            et for et in aggregated_event_configs
            if et not in self.triggers_funcs
        ]
        if missing_triggers:
            print(
                f"[Engine] [!!] 规则引用了未加载的触发器: {', '.join(missing_triggers)}",
                file=sys.stderr,
            )
            self._alert_user(
                "触发器缺失",
                f"以下触发器未装载，相关规则不会生效: {', '.join(missing_triggers)}",
            )

        # 启动触发器线程
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
            print(
                f"[Engine] 已启动触发器线程: {event_type}"
                f"（共监听 {len(config_list)} 条规则）"
            )

        if thread_count == 0:
            print("[Engine] 没有找到可用触发器，程序将退出。")
            self._alert_user(
                "NotmyFault 启动失败",
                "没有可用的触发器，请检查规则配置",
                open_dashboard=True,
            )
            return

        # 主循环：等待关闭信号 + 配置热重载
        se = self._shutdown_flag
        config_mtime = (
            os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0
        )
        self._hot_reload_error_reported = False  # 避免重复告警

        try:
            while not se.is_set():
                se.wait(1)

                # 配置热重载
                try:
                    new_mtime = (
                        os.path.getmtime(CONFIG_FILE)
                        if os.path.exists(CONFIG_FILE)
                        else 0
                    )
                    if new_mtime > config_mtime:
                        config_mtime = new_mtime
                        with open(CONFIG_FILE, "r", encoding="utf-8") as _f:
                            _new = json.load(_f)
                        with self._rules_lock:
                            self.rules = _new.get("rules", [])
                        print(
                            f"[Engine] 配置已热加载（{len(self.rules)} 条规则）"
                        )
                        self._hot_reload_error_reported = False
                        # 重新校验规则
                        self._validate_all_rules()
                except json.JSONDecodeError as e:
                    self._diag["hot_reload_errors"] += 1
                    engine_error("hot_reload_error", error=str(e))
                    print(
                        f"[Engine] 热加载配置 JSON 解析失败: {e}",
                        file=sys.stderr,
                    )
                    if not self._hot_reload_error_reported:
                        self._hot_reload_error_reported = True
                        self._alert_user(
                            "配置格式错误",
                            f"config.json 存在 JSON 语法错误，热加载失败，请修正后保存",
                        )
                except OSError as e:
                    print(
                        f"[Engine] 读取配置文件失败: {e}",
                        file=sys.stderr,
                    )
                except Exception:
                    print(
                        "[Engine] 热加载配置失败:",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
        except KeyboardInterrupt:
            print("[Engine] 主程序收到中断，退出中...")

        # 优雅关闭
        self._shutdown_plugins()
        self._wait_active_actions()

    def _shutdown_plugins(self) -> None:
        """调用所有已加载插件的 teardown() 钩子。"""
        for plugin_id, module in self._plugin_modules.items():
            if hasattr(module, "teardown"):
                try:
                    print(f"[Engine] 调用 teardown: {plugin_id}")
                    module.teardown()
                except Exception:
                    print(
                        f"[Engine] teardown \"{plugin_id}\" 异常:",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)

    def _wait_active_actions(self) -> None:
        """等待所有活跃动作完成。"""
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

    def shutdown(self) -> None:
        """外部关闭入口（供 API 层调用）。"""
        if self._shutdown_flag:
            self._shutdown_flag.set()
        self._shutdown_plugins()
        self._wait_active_actions()
