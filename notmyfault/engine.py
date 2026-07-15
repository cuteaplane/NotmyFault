import ast
import importlib.util
import inspect
import json
import os
import sys
import threading
import time
import datetime
from enum import Enum
import secrets
import subprocess
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from notmyfault.config import CONFIG_FILE
from notmyfault.logging import engine_info, engine_warn, engine_error
from notmyfault.plugin_schema import validate_plugin_meta

_validate_plugin_meta = validate_plugin_meta  # 向后兼容旧导入


_PLUGIN_MANIFEST_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "plugin_manifest.json")


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


def _compute_file_hash(file_path: str) -> str | None:
    import hashlib
    try:
        with open(file_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None

def _load_plugin_manifest() -> dict[str, dict[str, str]]:
    try:
        if os.path.exists(_PLUGIN_MANIFEST_FILE):
            with open(_PLUGIN_MANIFEST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}

def _save_plugin_manifest(manifest: dict[str, dict[str, str]]) -> None:
    try:
        os.makedirs(os.path.dirname(_PLUGIN_MANIFEST_FILE), exist_ok=True)
        with open(_PLUGIN_MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
    except OSError:
        pass

def _verify_plugin_integrity(plugin_id: str, files: list[tuple[str, str]]) -> tuple[bool, str]:
    manifest = _load_plugin_manifest()
    existing = manifest.get(plugin_id, {})
    all_match = True
    messages: list[str] = []
    for file_type, file_path in files:
        current_hash = _compute_file_hash(file_path)
        if current_hash is None:
            messages.append("无法读取 " + file_type)
            all_match = False
            continue
        if plugin_id in manifest:
            expected_hash = existing.get(file_type)
            if expected_hash is not None and current_hash != expected_hash:
                messages.append(file_type + " 文件已被修改！（期望 " + expected_hash[:12] + "...）")
                all_match = False
        if plugin_id not in manifest:
            manifest[plugin_id] = {}
        manifest[plugin_id][file_type] = current_hash
    _save_plugin_manifest(manifest)
    if not all_match:
        return False, "；".join(messages)
    return True, "完整性校验通过"



# ---------------------------------------------------------------------------
# 插件签名校验
# ---------------------------------------------------------------------------

def _verify_plugin_sig(plugin_dir, origin="builtin"):
    if origin != "builtin":
        return True
    try:
        from notmyfault.signing_keys import get_public_keys
        pub_keys = get_public_keys()
        if not pub_keys:
            return False
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pubs = [Ed25519PublicKey.from_public_bytes(k) for k in pub_keys]
    except ImportError:
        return False
    import hashlib
    sig_file = os.path.join(plugin_dir, "signature.sig")
    if not os.path.exists(sig_file):
        return False
    with open(sig_file, "rb") as f:
        sig = f.read()
    files = sorted(os.listdir(plugin_dir))
    payload = b""
    for name in files:
        if name == "signature.sig" or name.startswith("."):
            continue
        fp = os.path.join(plugin_dir, name)
        if os.path.isfile(fp):
            with open(fp, "rb") as f:
                payload += f.read()
    digest = hashlib.sha256(payload).digest()
    for pub in pubs:
        try:
            pub.verify(sig, digest)
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# AutomationEngine
# ---------------------------------------------------------------------------


class SecurityMode(Enum):
    STRICT = "strict"
    NORMAL = "normal"
    PERMISSIVE = "permissive"


def _detect_security_mode() -> SecurityMode:
    env_mode = os.environ.get("NOTMYFAULT_MODE", "").lower().strip()
    if env_mode == "alpha":
        return SecurityMode.PERMISSIVE
    elif env_mode in ("develop", "dev"):
        return SecurityMode.NORMAL
    elif env_mode in ("stable", "master"):
        return SecurityMode.STRICT

    import json as _j
    _paths = []
    if getattr(sys, "frozen", False):
        _paths.append(os.path.join(sys._MEIPASS, "build.json"))
    _paths += [
        os.path.join(os.path.dirname(__file__), "..", "build.json"),
        os.path.join(os.getcwd(), "build.json"),
    ]
    for _bp in _paths:
        try:
            _bj = _j.load(open(_bp, encoding="utf-8"))
            _m = _bj.get("security_mode", "").lower().strip()
            if _m == "permissive":
                return SecurityMode.PERMISSIVE
            elif _m == "normal":
                return SecurityMode.NORMAL
            elif _m == "strict":
                return SecurityMode.STRICT
        except Exception:
            continue

    try:
        import subprocess as _sp
        r = _sp.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=3)
        branch = r.stdout.strip()
        if branch in ("master",):
            return SecurityMode.STRICT
        elif branch in ("develop",):
            return SecurityMode.NORMAL
        elif branch in ("develop-alpha",):
            return SecurityMode.PERMISSIVE
    except Exception:
        pass
    return SecurityMode.STRICT

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

        # 安全系统
        self._engine_token: str = secrets.token_hex(32)
        from notmyfault import sudo as _sudo
        _sudo.set_engine_token(self._engine_token)
        self._sudo = _sudo
        self._security_mode = _detect_security_mode()
        engine_info(f"Security mode: {self._security_mode.value}")
        self._plugin_integrity_errors: list[str] = []

        # 优雅关闭
        self._active_actions = 0
        self._action_lock = threading.Lock()
        self._action_done = threading.Condition()
        self._shutdown_flag: "threading.Event | None" = None

        # 规则热重载线程安全
        self._rules_lock = threading.RLock()

        # 触发器线程管理
        self._trigger_threads: Dict[str, threading.Thread] = {}
        self._trigger_events: Dict[str, threading.Event] = {}

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
                "admin_plugins": self._sudo.get_authorized_plugins(),
                "integrity_errors": self._plugin_integrity_errors[-10:],
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
    # 条件匹配助手
    # ------------------------------------------------------------------

    @staticmethod
    def _get_rule_events(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从规则中提取所有事件条件。

        新格式: rule.condition.events → event列表
        旧格式: rule.event → 单个事件包装为列表
        """
        condition = rule.get("condition")
        if condition is not None and isinstance(condition, dict):
            events = condition.get("events", [])
            if isinstance(events, list) and events:
                return events
        event = rule.get("event") or rule.get("trigger")
        if event and isinstance(event, dict):
            return [event]
        return []

    @staticmethod
    def _check_event_params(event_def: Dict[str, Any], event_payload: Dict[str, Any]) -> bool:
        """检查事件payload是否匹配事件定义的参数。"""
        expected_params = event_def.get("params", {})
        for key, expected_val in expected_params.items():
            if event_payload.get(key) != expected_val:
                return False
        return True



    # ------------------------------------------------------------------
    # 插件加载
    # ------------------------------------------------------------------

    def auto_load(self, load_paths) -> None:
        """扫描并加载所有插件。支持 [(base_dir, origin), ...] 或兼容单字符串。"""
        if isinstance(load_paths, str):
            load_paths = [(load_paths, "builtin")]
        engine_info("=== SESSION_START ===")
        t_loaded = t_failed = a_loaded = a_failed = 0
        for base_dir, origin in load_paths:
            _t, _tf = self._load_plugins(
                base_dir=base_dir,
                plugins_dir="triggers",
                json_filename="trigger.json",
                py_filename="trigger.py",
                module_prefix="notmyfault.trigger_",
                meta_store=self.triggers_meta,
                func_store=self.triggers_funcs,
                store_name="Trigger",
                origin=origin,
            )
            _a, _af = self._load_plugins(
                base_dir=base_dir,
                plugins_dir="actions",
                json_filename="action.json",
                py_filename="action.py",
                module_prefix="notmyfault.action_",
                meta_store=self.actions_meta,
                func_store=self.actions_funcs,
                store_name="Actioner",
                origin=origin,
            )
            t_loaded += _t; t_failed += _tf
            a_loaded += _a; a_failed += _af

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
        origin: str = "builtin",
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
            is_valid, errors = validate_plugin_meta(meta, plugin_type)
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

            if origin == "builtin":
                disabled_cfg = self.config.get("disabled_plugins", {})
                ptype_key = "triggers" if store_name == "Trigger" else "actions"
                disabled_list = disabled_cfg.get(ptype_key, []) if isinstance(disabled_cfg, dict) else []
                if plugin_id in disabled_list:
                    print(
                        f"[Engine] 插件 \"{plugin_id}\" ({meta["name"]}) 已被用户禁用，跳过"
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

            # --- 插件完整性校验 ---
            integrity_files = [
                (json_filename, json_file),
                (py_filename, py_file),
            ]
            integrity_ok, integrity_msg = _verify_plugin_integrity(plugin_id, integrity_files)
            if not integrity_ok:
                warning = (f"[Engine] [安全] 插件 \"{plugin_id}\" 完整性校验失败：" + integrity_msg)
                print(warning, file=sys.stderr)
                engine_warn(f"integrity_check: {integrity_msg}")
                self._plugin_integrity_errors.append(warning)

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

            # --- 签名校验（仅 builtin） ---
            if origin == "builtin" and not _verify_plugin_sig(folder_path, origin):
                if self._security_mode == SecurityMode.STRICT:
                    print(f"[Engine] [!!] {store_name} \"{plugin_id}\" 签名无效，不加载", file=sys.stderr)
                    continue
                elif self._security_mode == SecurityMode.NORMAL:
                    print(f"[Engine] [!!] {store_name} \"{plugin_id}\" 签名无效，降级加载", file=sys.stderr)
            # --- 同名覆盖 ---
            if plugin_id in self._plugin_modules:
                old_origin = meta_store.get(plugin_id, {}).get("origin", "builtin")
                engine_info(f"{store_name} \"{plugin_id}\": {old_origin} -> {origin} override")
                old_module = self._plugin_modules[plugin_id]
                if hasattr(old_module, "teardown"):
                    old_module.teardown()
                self._plugin_modules.pop(plugin_id, None)
                func_store.pop(plugin_id, None)
                meta_store.pop(plugin_id, None)
            func_store[plugin_id] = getattr(module, "run")
            meta_store[plugin_id] = {**meta, "origin": origin}
            self._plugin_modules[plugin_id] = module

            # --- Admin 权限注册 ---
            if "admin" in (meta.get("permissions") or []):
                try:
                    self._sudo.authorize_plugin(plugin_id, self._engine_token)
                    print(f"[Engine] [安全] 插件 \"{plugin_id}\" 已注册管理员权限")
                except PermissionError as e:
                    print(f"[Engine] [!!] 插件 \"{plugin_id}\" 管理员权限注册失败: {e}", file=sys.stderr)
                    engine_error("admin_registration_failed", plugin=plugin_id, error=str(e))

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
            rule_events = self._get_rule_events(rule)
            all_events_valid = True
            for event_def in rule_events:
                event_type = event_def.get("type", "")
                if event_type and event_type not in self.triggers_meta:
                    issue = f"引用了未加载的触发器: {event_type}"
                    print(
                        f"[Engine] [!!] 规则 \"{rule_name}\" {issue}",
                        file=sys.stderr,
                    )
                    self._diag["rule_issues"].append((rule_name, issue))
                    engine_error("rule_issue", rule=rule_name, issue=issue)
                    all_events_valid = False
            if not all_events_valid:
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
            events = self._get_rule_events(rule)
            matched = False
            for event_def in events:
                if event_def.get("type") != event_type:
                    continue
                if self._check_event_params(event_def, event_payload):
                    matched = True
                    break
            if not matched:
                continue
            
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
    # 触发器线程管理
    # ------------------------------------------------------------------

    def _start_trigger_threads(self, rules: List[Dict[str, Any]] | None = None) -> int:
        if rules is None:
            with self._rules_lock:
                rules = list(self.rules)

        aggregated: Dict[str, List[Dict[str, Any]]] = {}
        for rule in rules:
            rule_events = self._get_rule_events(rule)
            for event_def in rule_events:
                event_type = event_def.get("type")
                if not event_type:
                    continue
                aggregated.setdefault(event_type, []).append(event_def.get("params", {}))

        missing = [et for et in aggregated if et not in self.triggers_funcs]
        if missing:
            print(
                f"[Engine] [!!] 规则引用了未加载的触发器: {', '.join(missing)}",
                file=sys.stderr,
            )
            self._alert_user(
                "触发器缺失",
                f"以下触发器未装载，相关规则不会生效: {', '.join(missing)}",
            )

        count = 0
        for event_type, config_list in aggregated.items():
            if event_type not in self.triggers_funcs:
                continue

            trigger_meta = self.triggers_meta.get(event_type, {})
            trigger_func = self.triggers_funcs[event_type]
            trigger_event = threading.Event()
            self._trigger_events[event_type] = trigger_event

            thread = threading.Thread(
                target=trigger_func,
                args=(trigger_meta, config_list, self.emit_event, trigger_event),
                daemon=True,
            )
            thread.start()
            self._trigger_threads[event_type] = thread
            count += 1
            print(
                f"[Engine] 已启动触发器线程: {event_type}"
                f"（共监听 {len(config_list)} 条规则）"
            )

        return count

    def _stop_trigger_threads(self, timeout: float = 30.0) -> None:
        """设置所有触发器关闭事件，等待线程退出（最多 timeout 秒）。"""
        if not self._trigger_threads:
            return

        for evt in self._trigger_events.values():
            evt.set()

        deadline = time.time() + timeout
        for event_type, thread in list(self._trigger_threads.items()):
            remaining = deadline - time.time()
            if remaining > 0:
                thread.join(timeout=remaining)
            if thread.is_alive():
                print(
                    f"[Engine] [!!] 触发器线程 {event_type} 未在 {timeout}s 内退出，强制终止",
                    file=sys.stderr,
                )

        self._trigger_threads.clear()
        self._trigger_events.clear()

    # ------------------------------------------------------------------
    # 引擎生命周期
    # ------------------------------------------------------------------

    def start(
        self, shutdown_event: "threading.Event | None" = None
    ) -> None:
        self._start_time = time.time()
        self._shutdown_flag = shutdown_event or threading.Event()

        self._validate_all_rules()

        from Win_toaster.show_notification import show_notification
        from Win_toaster.AUMID_Register import register_toaster

        register_toaster()
        show_notification("NotmyFault 已加载", "")

        thread_count = self._start_trigger_threads()

        if thread_count == 0:
            print("[Engine] 没有找到可用触发器，程序将退出。")
            self._alert_user(
                "NotmyFault 启动失败",
                "没有可用的触发器，请检查规则配置",
                open_dashboard=True,
            )
            return

        se = self._shutdown_flag
        config_mtime = (
            os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0
        )
        self._hot_reload_error_reported = False

        try:
            while not se.is_set():
                se.wait(1)

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
                        new_rules = _new.get("rules", [])

                        with self._rules_lock:
                            old_rule_count = len(self.rules)
                            self.rules = new_rules

                        print(
                            f"[Engine] 配置已热加载（{old_rule_count} → {len(new_rules)} 条规则）"
                        )
                        self._hot_reload_error_reported = False
                        self._validate_all_rules()

                        self._stop_trigger_threads(timeout=30)
                        started = self._start_trigger_threads(new_rules)
                        if started == 0:
                            print("[Engine] 热加载后无可用触发器，保持引擎运行")
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

        self._stop_trigger_threads(timeout=30)
        self._shutdown_plugins()
        self._wait_active_actions()

    def _shutdown_plugins(self) -> None:
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
        if self._shutdown_flag:
            self._shutdown_flag.set()
        for evt in self._trigger_events.values():
            evt.set()
        self._stop_trigger_threads(timeout=30)
        self._shutdown_plugins()
        self._wait_active_actions()
