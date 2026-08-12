"""提供本地 REST API 和 SSE 事件流，供 Dashboard 控制并读取引擎状态。"""

import json
import secrets
import os
import re
import sys
import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Protocol

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

from notmyfault.core.bindings import iter_legacy_event_payload_paths, iter_references
from notmyfault.core.run_history import RunHistory
from notmyfault.core.rule_drafting import draft_rule_from_text
from notmyfault.components.session import ComponentSessionManager
from notmyfault.extensions.protocol import (
    OwnedValueError,
    owned_value_identity,
    unpack_owned_value,
)
from notmyfault.extensions.session import ExtensionContext, ExtensionSessionManager
from notmyfault.config import (
    ADMIN_AUTHORIZATION_MODES,
    CONFIG_FILE,
    ConfigValidationError,
    RULES_FILE,
    ensure_rule_binding_ids,
    ensure_rule_id,
    get_admin_authorization_mode,
    load_verified_config,
    load_verified_rules,
    save_config as config_save,
    save_rules as rules_save,
)
from notmyfault.platform.platform_support import get_config_dir
from notmyfault.security.plugin_schema import (
    scan_plugins,
    validate_plugin_meta,
    scan_plugin_security,
    check_permissions_conform,
    get_permission_info,
    is_known_permission,
    current_platform_name,
    check_payload_contract,
    PERMISSION_REGISTRY,
)
from notmyfault.security.plugins import scan_borrowed_privilege
from notmyfault.security.security import detect_security_mode, SecurityMode
from notmyfault.core.rules import (
    get_rule_events,
    validate_rule_bindings,
    validate_rules,
    validate_rules_structure,
)
from notmyfault.version import __version__

_scan_plugins = scan_plugins  # 旧调用仍通过此别名访问插件扫描函数。

# 插件目录位于包根，开发签名密钥位于项目根，两个路径都由当前文件定位。
_PKG_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PRIVATE_DIR = _PROJECT_ROOT / ".private"

# API 认证令牌由 Dashboard 和后台服务共享，服务重启后仍从磁盘读取。
API_TOKEN: str = ""
# TEMP 默认允许 Authenticated Users 读取，因此 token 与 config.json 同目录。
API_TOKEN_FILE: str = os.path.join(os.path.dirname(CONFIG_FILE), ".api_token")
# dashboard.pyw 端口从 19199 起选择，候选地址全部绑定 loopback。
_DASHBOARD_ORIGINS = [
    f"http://{host}:{port}"
    for host in ("127.0.0.1", "localhost")
    for port in range(19199, 19219)
]

# py7zr 仅处理绝对路径，条目数和解压体积由调用方在解压前检查。
_NMFP_MAX_ENTRIES = 2000
_NMFP_MAX_UNCOMPRESSED = 500 * 1024 * 1024  # 500 MiB
_NMFP_MAX_UPLOAD_BYTES = 64 * 1024 * 1024   # 64 MiB
_TEST_CONTEXT_MAX_BYTES = 1024 * 1024
_EXTENSION_MESSAGE_MAX_BYTES = 1024 * 1024
_TEST_ASSERTION_MAX_COUNT = 50
_TEST_ASSERTION_OPERATORS = frozenset(
    {"equals", "contains", "gt", "gte", "lt", "lte", "exists"}
)
# 归档里的符号链接指向可执行文件时拒绝安装，链接目标不受签名和目录边界约束。
_NMFP_EXECUTABLE_EXTENSIONS = frozenset(
    {".exe", ".dll", ".bin", ".com", ".scr", ".sys", ".msi", ".cpl"}
)


def _extract_nmfp_safely(archive_path: str, extract_dir: str, password: str | None) -> None:
    """解压 nmfp 插件包并检查路径、条目数和解压体积，发现违规时抛 ValueError。"""
    import py7zr as _py7zr
    with _py7zr.SevenZipFile(archive_path, mode="r", password=password or None) as zf:
        entries = 0
        total_uncompressed = 0
        for info in zf.list():
            entries += 1
            if entries > _NMFP_MAX_ENTRIES:
                raise ValueError(
                    f"插件包条目过多（>{_NMFP_MAX_ENTRIES}），疑似解压炸弹"
                )
            total_uncompressed += int(getattr(info, "uncompressed", 0) or 0)
            if total_uncompressed > _NMFP_MAX_UNCOMPRESSED:
                raise ValueError(
                    "插件包解压后体积过大，疑似解压炸弹"
                )
            name = str(getattr(info, "filename", ""))
            normalized = os.path.normpath(name)
            if (
                normalized.startswith("..")
                or os.path.isabs(normalized)
                or re.match(r"^[a-zA-Z]:", normalized)
            ):
                raise ValueError(f"插件包包含非法路径: {name}")
            if getattr(info, "is_symlink", False):
                extension = os.path.splitext(normalized)[1].lower()
                if extension in _NMFP_EXECUTABLE_EXTENSIONS:
                    raise ValueError(f"插件包包含可执行文件的符号链接: {name}")
        zf.extractall(extract_dir)

    # 解压后再扫一遍磁盘，拦住归档元数据没有标记 symlink 的漏网条目。
    for base, _dirs, file_names in os.walk(extract_dir):
        for file_name in file_names:
            full_path = os.path.join(base, file_name)
            extension = os.path.splitext(file_name)[1].lower()
            if os.path.islink(full_path) and extension in _NMFP_EXECUTABLE_EXTENSIONS:
                raise ValueError(f"插件包包含可执行文件的符号链接: {file_name}")


def _run_plugin_build_hook(root_path: str, meta: dict) -> None:
    """插件声明 build 钩子时在插件目录执行编译命令并校验产物，失败抛 ValueError。"""
    build = meta.get("build")
    if not isinstance(build, dict):
        return
    command = build.get("command") or []
    outputs = build.get("outputs") or []
    if not command and not outputs:
        return
    import subprocess
    for step in command:
        try:
            result = subprocess.run(
                step,
                shell=True,
                cwd=root_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise ValueError(f"build 命令超时（120s）: {step}")
        except OSError as error:
            raise ValueError(f"build 命令无法执行: {step} ({error})")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-300:]
            raise ValueError(
                f"build 命令失败（rc={result.returncode}）: {step} {detail}"
            )
    for relative_output in outputs:
        if not os.path.exists(os.path.join(root_path, relative_output)):
            raise ValueError(f"build 产物不存在: {relative_output}")
    # 现场编译产物不继承归档内任何签名，按未签名插件降级处理。
    for signature_name in ("signature.sig", "public_key.pem"):
        signature_path = os.path.join(root_path, signature_name)
        if os.path.exists(signature_path):
            os.unlink(signature_path)


def _secure_write_token(path: str, token: str) -> None:
    """写入 token 文件并设置当前用户权限。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    try:
        # 先写临时文件再原子替换，读取方拿到完整 token。
        tmp_path = path + ".tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.replace(tmp_path, path)
        # Windows 上用 icacls 移除继承权限，只授予当前用户完全控制。
        if os.name == "nt":
            try:
                import subprocess as _sp
                userdomain = os.environ.get("USERDOMAIN", "")
                username = os.environ.get("USERNAME") or os.getlogin()
                full_user = f"{userdomain}\\{username}" if userdomain else username
                r1 = _sp.run(
                    ["icacls", path, "/grant:r", f"{full_user}:F"],
                    capture_output=True, timeout=5,
                )
                if r1.returncode == 0:
                    _sp.run(
                        ["icacls", path, "/inheritance:r"],
                        capture_output=True, timeout=5,
                    )
            except Exception:
                pass
    except OSError:
        pass


def _load_or_create_api_token(path: str) -> str:
    """读取 token，文件缺失或内容损坏时生成新值。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if len(token) == 64:
            int(token, 16)
            return token
    except (OSError, ValueError):
        pass
    token = secrets.token_hex(32)
    _secure_write_token(path, token)
    return token



class EngineRunnerLike(Protocol):
    """api_server 期望的引擎运行器接口"""
    engine_running: bool
    engine_state: str
    shutdown_event: Any  # threading.Event
    engine_thread: Any   # threading.Thread | None
    current_engine: Any

    def start_engine(self) -> bool: ...
    def stop_engine(self) -> bool: ...
    def request_process_shutdown(self, force_after: float = 10) -> None: ...


class EngineAPI:
    """引擎 HTTP API 服务，提供控制端点和 SSE 事件流。"""

    def __init__(self, engine_runner: EngineRunnerLike):
        self._engine = engine_runner
        self._subscribers: list["asyncio.Queue"] = []

        self._loop = None
        self._sub_lock = threading.Lock()
        self._server = None
        self._run_history = RunHistory(
            os.path.join(os.path.dirname(CONFIG_FILE), "run-events.jsonl")
        )
        # 旧测试宿主可注入实例，正式运行时从 EngineRunner.current_engine 读取。
        self._engine_ref = None
        self._component_sessions = ComponentSessionManager()
        self._extension_sessions = ExtensionSessionManager()

        # 预览 token 映射到解压目录、根路径、元数据、类型和创建时间。
        self._pending_previews: Dict[str, Any] = {}

        self.app = FastAPI(title="NotmyFault Engine API", version=__version__)
        self._setup_middleware()
        self._setup_routes()
        global API_TOKEN
        API_TOKEN = _load_or_create_api_token(API_TOKEN_FILE)

    def _setup_middleware(self):
        self.app.add_middleware(
            CORSMiddleware,
            # 只允许 Dashboard 的本机来源读取自动化数据。
            allow_origins=_DASHBOARD_ORIGINS,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self.app.middleware("http")(self._auth_middleware)

    async def _auth_middleware(self, request: Request, call_next):
        """统一校验 /api 路由的认证信息。"""
        if request.url.path.startswith("/api/"):
            try:
                await self._verify_auth(request)
            except HTTPException as exc:
                # 中间件外抛出的 HTTPException 需要手动转换成响应。
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    def push_event(self, event_type: str, data: Dict[str, Any]):
        packet = {"type": event_type, "data": data, "ts": time.time()}
        try:
            self._run_history.record(packet)
        except (OSError, TypeError, ValueError):
            pass
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._sub_lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            def _deliver(q=q, packet=packet):
                try:
                    q.put_nowait(packet)
                except Exception:
                    with self._sub_lock:
                        if q in self._subscribers:
                            self._subscribers.remove(q)
            try:
                loop.call_soon_threadsafe(_deliver)
            except Exception:
                pass

    def _get_plugins_schema(self) -> Dict[str, Any]:
        base = str(_PKG_ROOT)
        result: Dict[str, Dict] = {
            "triggers": scan_plugins(base, "triggers", "trigger.json"),
            "actions": scan_plugins(base, "actions", "action.json"),
        }
        user_dir = self._get_user_plugins_dir()
        if os.path.isdir(user_dir):
            for ptype in ("triggers", "actions"):
                json_name = "trigger.json" if ptype == "triggers" else "action.json"
                for pid, meta in scan_plugins(user_dir, ptype, json_name).items():
                    if pid not in result[ptype]:
                        result[ptype][pid] = meta
        return result

    def _validate_rule_draft(self, rule: Any) -> Dict[str, Any]:
        issues: List[Dict[str, Any]] = []

        def add(
            severity: str,
            code: str,
            message: str,
            location: str = "",
        ) -> None:
            item = {
                "severity": severity,
                "code": code,
                "message": message,
            }
            if location:
                item["location"] = location
            issues.append(item)

        structure_errors = validate_rules_structure([rule])
        if structure_errors:
            for message in structure_errors[:20]:
                add("error", "invalid_structure", message)
        else:
            normalized = ensure_rule_binding_ids(rule)
            schema = self._get_plugins_schema()
            _valid, _total, _plugin_errors, plugin_warnings = validate_rules(
                [normalized],
                schema["triggers"],
                schema["actions"],
            )
            for _rule_name, message in plugin_warnings:
                add("warning", "plugin_parameter", message)

            for event in get_rule_events(normalized):
                plugin = schema["triggers"].get(event.get("type", ""))
                if plugin is None:
                    add(
                        "error",
                        "plugin_reference",
                        f"引用了未加载的触发器: {event.get('type', '')}",
                        "event",
                    )
                elif plugin.get("platform_compatible") is False:
                    add(
                        "error",
                        "platform_incompatible",
                        f"触发器“{plugin.get('name') or event.get('type')}”不支持当前系统",
                        "event",
                    )

            for field in ("preconditions", "actions"):
                for index, item in enumerate(normalized.get(field, [])):
                    label = (
                        f"开始前确认 {index + 1}"
                        if field == "preconditions"
                        else f"动作 {index + 1}"
                    )
                    action_items = [(item, f"{field}[{index}]", label)]
                    if field == "actions":
                        action_items.extend(
                            (
                                failure_action,
                                f"actions[{index}].failure_actions[{failure_index}]",
                                f"动作 {index + 1} 的补救动作 {failure_index + 1}",
                            )
                            for failure_index, failure_action in enumerate(
                                item.get("failure_actions", [])
                            )
                        )
                    for action_item, location, label in action_items:
                        plugin = schema["actions"].get(action_item.get("type", ""))
                        if plugin is None:
                            add(
                                "error",
                                "plugin_reference",
                                f"{label}引用了未加载的动作: {action_item.get('type', '')}",
                                location,
                            )
                        elif plugin.get("platform_compatible") is False:
                            add(
                                "error",
                                "platform_incompatible",
                                f"{label}“{plugin.get('name') or action_item.get('type')}”不支持当前系统",
                                location,
                            )
                        elif (
                            action_item.get("timeout_seconds") is not None
                            and plugin.get("cancellation_api") != "runtime-v1"
                        ):
                            add(
                                "error",
                                "timeout_not_supported",
                                f"{label}不支持安全取消，不能设置运行超时",
                                location,
                            )
                        elif (
                            field == "actions"
                            and int(action_item.get("retry", 0) or 0) > 0
                            and plugin.get("idempotent") is not True
                        ):
                            add(
                                "warning",
                                "retry_may_repeat",
                                f"{label}“{plugin.get('name') or action_item.get('type')}”没有声明可安全重复执行，重试可能重复产生结果",
                                location,
                            )

            for issue in validate_rule_bindings(
                normalized,
                schema["triggers"],
                schema["actions"],
            ):
                add(
                    "error",
                    issue.get("code", "invalid_binding"),
                    issue.get("message", "规则数据绑定无效"),
                    issue.get("location", ""),
                )

            from notmyfault.config import _validate_rules_safety
            safety_warnings, safety_errors = _validate_rules_safety([normalized])
            for message in safety_warnings:
                add("warning", "safety_warning", message)
            for message in safety_errors:
                add("error", "unsafe_action", message)

        unique_issues = []
        seen = set()
        for issue in issues:
            key = (issue["severity"], issue["message"], issue.get("location", ""))
            if key not in seen:
                seen.add(key)
                unique_issues.append(issue)
        error_count = sum(item["severity"] == "error" for item in unique_issues)
        warning_count = sum(item["severity"] == "warning" for item in unique_issues)
        return {
            "ok": True,
            "valid": error_count == 0,
            "issues": unique_issues,
            "summary": {"errors": error_count, "warnings": warning_count},
        }

    def _get_user_plugins_dir(self) -> str:
        return os.path.join(get_config_dir(), "plugins")

    @staticmethod
    def _is_safe_plugin_id(pid: str) -> bool:
        """检查插件 id 不含路径分隔符或连续点。"""
        if not pid:
            return False
        if "/" in pid or "\\" in pid or ".." in pid:
            return False
        return True

    def _find_plugin_by_package(self, package_name: str):
        """按 package_name 查找用户插件，返回类型、id 和元数据。"""
        if not package_name:
            return None
        user_dir = self._get_user_plugins_dir()
        if not os.path.isdir(user_dir):
            return None
        for ptype in ("triggers", "actions"):
            json_name = "trigger.json" if ptype == "triggers" else "action.json"
            for pid, meta in scan_plugins(user_dir, ptype, json_name).items():
                if meta.get("package_name") == package_name:
                    return ptype, pid, meta
        return None

    def _list_all_plugins(self) -> Dict[str, Any]:
        base = str(_PKG_ROOT)
        user_dir = self._get_user_plugins_dir()

        config = self._load_config()
        disabled = config.get("disabled_plugins", {})
        if not isinstance(disabled, dict):
            disabled = {"triggers": [], "actions": []}

        result: Dict[str, Dict] = {"triggers": {}, "actions": {}}
        for ptype in ("triggers", "actions"):
            json_name = "trigger.json" if ptype == "triggers" else "action.json"
            disabled_set = set(disabled.get(ptype, []))

            builtin_plugins = scan_plugins(base, ptype, json_name)
            for pid, meta in builtin_plugins.items():
                meta["origin"] = "builtin"
                result[ptype][pid] = meta

            builtin_root = os.path.join(base, ptype)
            if os.path.isdir(builtin_root):
                for folder_name in sorted(os.listdir(builtin_root)):
                    json_path = os.path.join(builtin_root, folder_name, json_name)
                    if not os.path.exists(json_path):
                        continue
                    try:
                        with open(json_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        pid = meta.get("id")
                        if pid and pid not in result[ptype]:
                            meta["origin"] = "builtin"
                            result[ptype][pid] = meta
                    except Exception:
                        continue

            if os.path.isdir(user_dir):
                user_root = os.path.join(user_dir, ptype)
                # scan_plugins() 会跳过禁用或 schema 失败的插件，这里再扫描目录以便界面显示它们。
                for pid, meta in scan_plugins(user_dir, ptype, json_name).items():
                    meta["origin"] = "user"
                    result[ptype][pid] = meta

                if os.path.isdir(user_root):
                    plugin_type = "trigger" if ptype == "triggers" else "action"
                    for folder_name in sorted(os.listdir(user_root)):
                        json_path = os.path.join(user_root, folder_name, json_name)
                        if not os.path.exists(json_path):
                            continue
                        try:
                            with open(json_path, "r", encoding="utf-8") as f:
                                meta = json.load(f)
                        except (json.JSONDecodeError, OSError):
                            continue
                        pid = meta.get("id", folder_name)
                        if pid in result[ptype]:
                            continue
                        meta["origin"] = "user"
                        # schema 失败原因写入 _error，界面可以显示具体原因。
                        is_valid, errors = validate_plugin_meta(meta, plugin_type)
                        if not is_valid:
                            meta["_error"] = "schema: " + "; ".join(errors[:2])
                        result[ptype][pid] = meta

            # 禁用名单在内置和用户插件都扫完后统一套用，config 里的状态覆盖 json 里的 enabled。
            for pid in disabled_set:
                if pid in result[ptype]:
                    result[ptype][pid]["enabled"] = False

        # 把运行时诊断中的插件错误合并到列表。
        engine = self._resolve_current_engine()
        if engine is not None:
            diag = engine.get_diagnostics()
            for err in diag.get("plugins", {}).get("errors", []):
                # 每项错误依次包含类型、插件 id 和原因。
                if len(err) >= 3:
                    etype, epid, ereason = err[0], err[1], err[2]
                    cat = "triggers" if etype == "Trigger" else "actions"
                    if epid in result.get(cat, {}):
                        result[cat][epid]["_error"] = ereason

        return result

    def _resolve_current_engine(self):
        """从运行时控制器读取当前引擎实例。"""
        engine = getattr(self._engine, "current_engine", None)
        if engine is not None:
            return engine
        return self._engine_ref

    def _start_runtime(self) -> bool:
        start = getattr(self._engine, "start_engine", None)
        if callable(start):
            return start()
        return self._engine._start_engine_core()

    def _stop_runtime(self) -> bool:
        stop = getattr(self._engine, "stop_engine", None)
        if callable(stop):
            return stop()
        return self._engine._stop_engine()

    def _shutdown_runtime(self) -> None:
        shutdown = getattr(self._engine, "request_process_shutdown", None)
        if not callable(shutdown):
            shutdown = getattr(self._engine, "_request_process_shutdown", None)
        if callable(shutdown):
            shutdown()
            return
        self._stop_runtime()
        if self._server:
            self._server.should_exit = True

    def _toggle_plugin(self, ptype: str, pid: str) -> dict:
        if not self._is_safe_plugin_id(pid):
            return {"ok": False, "error": "插件 id 含非法字符（禁止路径分隔符）"}
        base = str(_PKG_ROOT)
        user_dir = self._get_user_plugins_dir()
        json_name = "trigger.json" if ptype == "triggers" else "action.json"

        # json 只用来确认插件存在和来源，trigger.json/action.json 带签名，改写会破坏签名。
        user_json = os.path.join(user_dir, ptype, pid, json_name)
        builtin_json = os.path.join(base, ptype, pid, json_name)
        if os.path.exists(user_json):
            origin = "user"
        elif os.path.exists(builtin_json):
            origin = "builtin"
        else:
            return {"ok": False, "error": "插件不存在"}

        try:
            config = self._load_config_for_update()
            disabled = config.get("disabled_plugins", {})
            if not isinstance(disabled, dict):
                disabled = {"triggers": [], "actions": []}
            disabled_list = disabled.get(ptype, [])
            if not isinstance(disabled_list, list):
                disabled_list = []
            if pid in disabled_list:
                disabled_list.remove(pid)
                new_enabled = True
            else:
                disabled_list.append(pid)
                new_enabled = False
            disabled[ptype] = disabled_list
            config["disabled_plugins"] = disabled
            ok = self._save_config(config)
            if not ok:
                return {"ok": False, "error": "无法保存配置"}
            return {"ok": True, "enabled": new_enabled,
                    "origin": origin,
                    "restart_required": True}
        except ConfigValidationError as e:
            return {"ok": False, "error": f"配置未通过完整性校验: {e}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _uninstall_plugin(self, ptype: str, pid: str) -> dict:
        if not self._is_safe_plugin_id(pid):
            return {"ok": False, "error": "插件 id 含非法字符（禁止路径分隔符）"}
        user_dir = self._get_user_plugins_dir()
        json_name = "trigger.json" if ptype == "triggers" else "action.json"
        plugin_dir = os.path.join(user_dir, ptype, pid)
        json_path = os.path.join(plugin_dir, json_name)

        if not os.path.exists(json_path):
            return {"ok": False, "error": "只能卸载用户插件，或插件不存在"}

        try:
            import shutil
            shutil.rmtree(plugin_dir)
            return {"ok": True, "restart_required": True}
        except OSError as e:
            return {"ok": False, "error": str(e)}


    def _load_config(self) -> Dict[str, Any]:
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    cfg.pop("_signature", None)
                    return cfg
        except json.JSONDecodeError:
            print(f"[API] 配置文件 JSON 格式错误，返回空规则列表", file=sys.stderr)
        except OSError as e:
            print(f"[API] 读取配置文件失败: {e}，返回空规则列表", file=sys.stderr)
        return {"rules": []}

    def _save_config(self, config: Dict[str, Any]) -> bool:
        return config_save(config)

    def _load_config_for_update(self) -> Dict[str, Any]:
        """首次写入创建空设置，已有设置必须先通过验签。"""
        if not os.path.exists(CONFIG_FILE):
            if not self._save_config({}):
                raise ConfigValidationError("无法创建配置文件")
        return load_verified_config()

    def _load_rules(self) -> List[Dict[str, Any]]:
        """读 rules.json 里的规则，文件缺失或损坏时返回空列表"""
        try:
            if os.path.exists(RULES_FILE):
                with open(RULES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                rules = data.get("rules", []) if isinstance(data, dict) else []
                return rules if isinstance(rules, list) else []
        except json.JSONDecodeError:
            print("[API] 规则文件 JSON 格式错误，返回空规则列表", file=sys.stderr)
        except OSError as e:
            print(f"[API] 读取规则文件失败: {e}，返回空规则列表", file=sys.stderr)
        return []

    def _save_rules(self, rules: List[Dict[str, Any]]) -> bool:
        return rules_save(rules)

    async def _verify_auth(self, request: Request) -> None:
        # CORS 预检不带凭据，交给 CORSMiddleware 处理。
        if request.method == "OPTIONS":
            return
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
        # 原生 EventSource 不能设置 Authorization，SSE 接口额外接受 query token。
        if not token and request.url.path == "/api/events":
            token = request.query_params.get("token", "")
        if not token or not secrets.compare_digest(token, API_TOKEN):
            # token 不匹配时修复磁盘文件，本次请求仍返回 403。
            self._repair_token_file()
            print(f"[Auth] 403 rejected: has_hdr={bool(auth)} req_len={len(token)} srv_len={len(API_TOKEN)}",
                  file=sys.stderr)
            raise HTTPException(status_code=403, detail="Forbidden: invalid API Token")

    @staticmethod
    def _repair_token_file() -> None:
        """把磁盘 token 改成正在提供服务的实例使用的值。"""
        try:
            with open(API_TOKEN_FILE, "r", encoding="utf-8") as f:
                if secrets.compare_digest(f.read().strip(), API_TOKEN):
                    return
        except OSError:
            pass
        _secure_write_token(API_TOKEN_FILE, API_TOKEN)

    def _setup_routes(self):
        app = self.app

        @app.post("/api/engine/start")
        async def engine_start(request: Request):
            await self._verify_auth(request)
            print("[API] POST /api/engine/start")
            current_state = getattr(
                self._engine,
                "engine_state",
                "running" if self._engine.engine_running else "stopped",
            )
            if current_state in ("running", "starting"):
                return {"ok": True, "running": self._engine.engine_running,
                        "engine_state": current_state,
                        "api_alive": True,
                        "message": "already_running" if current_state == "running" else "already_starting"}

            started = self._start_runtime()
            if started is False:
                return JSONResponse(
                    {"ok": False, "running": self._engine.engine_running,
                     "engine_state": getattr(self._engine, "engine_state", "stopping"),
                     "message": "engine_stopping"},
                    status_code=409,
                )
            return {
                "ok": True,
                "running": self._engine.engine_running,
                "engine_state": getattr(self._engine, "engine_state", "starting"),
                "api_alive": True,
            }

        @app.post("/api/engine/stop")
        async def engine_stop(request: Request):
            await self._verify_auth(request)
            print("[API] POST /api/engine/stop")
            stopped = self._stop_runtime()
            return {
                "ok": True,
                "stopped": stopped,
                "stopping": not stopped,
                "engine_state": getattr(self._engine, "engine_state", "stopped"),
                "api_alive": True,
            }

        @app.post("/api/engine/shutdown")
        async def engine_shutdown(request: Request):
            await self._verify_auth(request)
            """停止引擎并关闭 HTTP 服务。"""
            print("[API] POST /api/engine/shutdown")
            self._shutdown_runtime()

            return {"ok": True, "message": "shutting_down"}

        @app.get("/api/engine/status")
        async def engine_status():
            rules = self._load_rules()
            trigger_types = {
                event.get("type")
                for rule in rules
                if isinstance(rule, dict)
                for event in get_rule_events(rule)
                if event.get("type")
            }
            action_types = set()
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                actions = rule.get("actions", [])
                if not isinstance(actions, list):
                    continue
                action_types.update(
                    action.get("type")
                    for action in actions
                    if isinstance(action, dict) and action.get("type")
                )
            return {
                "running": self._engine.engine_running,
                "api_alive": True,
                "engine_running": self._engine.engine_running,
                "engine_state": getattr(
                    self._engine,
                    "engine_state",
                    "running" if self._engine.engine_running else "stopped",
                ),
                "pid": os.getpid(),
                "rules_count": len(rules),
                "triggers_count": len(trigger_types),
                "actions_count": len(action_types),
                "security_mode": detect_security_mode().value,
                "last_error": getattr(self._engine, "last_error", None),
            }

        @app.get("/api/platform")
        async def platform_status():
            if sys.platform.startswith("linux"):
                from notmyfault.platform.linux_support import capability_report
                return capability_report()
            return {
                "platform": "windows" if sys.platform == "win32" else sys.platform,
                "desktop": None,
                "session_type": None,
                "capabilities": {},
                "limitations": {},
            }

        @app.get("/api/rules")
        async def rules_list():
            return {"rules": self._load_rules()}

        @app.post("/api/rules/validate")
        async def rules_validate(request: Request):
            await self._verify_auth(request)
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    {"ok": False, "error": "无效的 JSON 请求体"},
                    status_code=400,
                )
            if not isinstance(body, dict) or "rule" not in body:
                return JSONResponse(
                    {"ok": False, "error": "请求体必须包含 rule 对象"},
                    status_code=400,
                )
            return self._validate_rule_draft(body["rule"])

        @app.post("/api/rules/draft")
        async def rules_draft(request: Request):
            await self._verify_auth(request)
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    {"ok": False, "error": "无效的 JSON 请求体"},
                    status_code=400,
                )
            if not isinstance(body, dict):
                return JSONResponse(
                    {"ok": False, "error": "请求体必须是对象"},
                    status_code=400,
                )
            result = draft_rule_from_text(
                body.get("description"),
                self._get_plugins_schema(),
            )
            if not result.get("ok"):
                return JSONResponse(result, status_code=400)
            draft = result.get("draft")
            if isinstance(draft, dict):
                draft = ensure_rule_binding_ids(draft)
                event = draft.get("event", {})
                if event.get("type") == "usb_insert":
                    trigger_id = event.get("binding_id", "")
                    for action in draft.get("actions", []):
                        if (
                            action.get("type") == "file_operation"
                            and not action.get("params", {}).get("source")
                            and trigger_id
                        ):
                            action["params"]["source"] = {
                                "$ref": {
                                    "scope": "trigger",
                                    "node": trigger_id,
                                    "path": ["actual_drive"],
                                }
                            }
                            result["missing"] = [
                                item for item in result.get("missing", [])
                                if "源路径" not in item
                            ]
                            result.setdefault("assumptions", []).append(
                                "文件来源使用本次插入的 U 盘"
                            )
                result["draft"] = draft
                result["validation"] = self._validate_rule_draft(draft)
            return result

        @app.put("/api/rules")
        async def rules_save(request: Request):
            await self._verify_auth(request)
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    {"ok": False, "error": "无效的 JSON 请求体"},
                    status_code=400,
                )

            if not isinstance(body, dict):
                return JSONResponse(
                    {"ok": False, "error": "请求体必须是 JSON 对象"},
                    status_code=400,
                )

            rules = body.get("rules")
            structure_errors = validate_rules_structure(rules)
            if structure_errors:
                return JSONResponse(
                    {"ok": False, "error": "规则结构校验失败",
                     "details": structure_errors[:10]},
                    status_code=400,
                )
            if not isinstance(rules, list):
                return JSONResponse(
                    {"ok": False, "error": "rules 必须是列表"},
                    status_code=400,
                )
            seen_rule_ids: set[str] = set()
            normalized_rules = [
                ensure_rule_binding_ids(ensure_rule_id(rule, seen_rule_ids))
                for rule in rules
            ]
            structure_errors = validate_rules_structure(normalized_rules)
            if structure_errors:
                return JSONResponse(
                    {"ok": False, "error": "规则节点标识无效",
                     "details": structure_errors[:10]},
                    status_code=400,
                )
            schema = self._get_plugins_schema()
            binding_issues = []
            for index, rule in enumerate(normalized_rules):
                for issue in validate_rule_bindings(
                    rule,
                    schema["triggers"],
                    schema["actions"],
                ):
                    binding_issues.append({
                        "rule": rule.get("name", f"规则 #{index + 1}"),
                        **issue,
                    })
            if binding_issues:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "规则数据绑定无效",
                        "details": binding_issues[:20],
                    },
                    status_code=400,
                )

            # 命中危险命令或路径时拒绝写入规则。
            from notmyfault.config import _validate_rules_safety
            _warnings, errors = _validate_rules_safety(normalized_rules)
            if errors:
                return JSONResponse(
                    {"ok": False, "error": "规则安全校验失败", "details": errors[:10]},
                    status_code=400,
                )

            if os.path.exists(RULES_FILE):
                try:
                    load_verified_rules()
                except ConfigValidationError as error:
                    return JSONResponse(
                        {"ok": False, "error": f"现有规则未通过完整性校验: {error}"},
                        status_code=409,
                    )

            # 规则只落 rules.json，不再连带重写设置文件
            ok = self._save_rules(normalized_rules)
            if ok:
                print(f"[API] 规则已保存 ({len(normalized_rules)} 条)")
                return {"ok": True, "rules": normalized_rules}
            else:
                return JSONResponse(
                    {"ok": False, "error": "写入规则文件失败"},
                    status_code=500,
                )

        @app.post("/api/rules/{rule_index}/run")
        async def rules_run(rule_index: int, request: Request):
            engine = self._resolve_current_engine()
            if engine is None:
                return JSONResponse(
                    {"ok": False, "error": "引擎尚未就绪"}, status_code=409,
                )
            try:
                verified_rules = load_verified_rules()
            except ConfigValidationError as error:
                return JSONResponse(
                    {"ok": False, "error": f"规则未通过完整性校验: {error}"},
                    status_code=409,
                )
            rule_snapshot = None
            has_snapshot = False
            trigger_payloads: Dict[str, Dict[str, Any]] = {}
            event_payload = None
            step_outputs: Dict[str, Any] = {}
            start_step_id = ""
            end_step_id = ""
            test_assertions: List[Dict[str, Any]] = []
            raw_body = await request.body()
            if raw_body:
                if len(raw_body) > _TEST_CONTEXT_MAX_BYTES:
                    return JSONResponse(
                        {"ok": False, "error": "测试数据超过 1 MiB 上限"},
                        status_code=413,
                    )
                try:
                    body = json.loads(raw_body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return JSONResponse(
                        {"ok": False, "error": "无效的 JSON 请求体"},
                        status_code=400,
                    )
                if not isinstance(body, dict):
                    return JSONResponse(
                        {"ok": False, "error": "请求体必须是 JSON 对象"},
                        status_code=400,
                    )
                if "rule" in body:
                    has_snapshot = True
                    rule_snapshot = body["rule"]
                if isinstance(body.get("trigger_payloads"), dict):
                    trigger_payloads = body["trigger_payloads"]
                if isinstance(body.get("event_payload"), dict):
                    event_payload = body["event_payload"]
                if "step_outputs" in body:
                    step_outputs = body["step_outputs"]
                start_step_id = body.get("start_step_id", "")
                end_step_id = body.get("end_step_id", "")
                if "test_assertions" in body:
                    test_assertions = body["test_assertions"]

            candidate_rule = None
            if has_snapshot:
                structure_errors = validate_rules_structure([rule_snapshot])
                if structure_errors:
                    return JSONResponse(
                        {"ok": False, "error": "规则结构校验失败",
                         "details": structure_errors[:10]},
                        status_code=400,
                    )
                disk_rules = verified_rules
                if (
                    not isinstance(disk_rules, list)
                    or rule_index < 0
                    or rule_index >= len(disk_rules)
                    or disk_rules[rule_index] != rule_snapshot
                ):
                    return JSONResponse(
                        {"ok": False, "error": "规则保存版本已变化，请刷新后重试"},
                        status_code=409,
                    )
                candidate_rule = rule_snapshot
            else:
                disk_rules = verified_rules
                if (
                    not isinstance(disk_rules, list)
                    or rule_index < 0
                    or rule_index >= len(disk_rules)
                ):
                    return JSONResponse(
                        {"ok": False, "error": "规则不存在"},
                        status_code=404,
                    )
                candidate_rule = disk_rules[rule_index]

            assert isinstance(candidate_rule, dict)
            actions = candidate_rule.get("actions", [])
            if not isinstance(actions, list):
                actions = []
            action_ids = [
                action.get("binding_id", "") if isinstance(action, dict) else ""
                for action in actions
            ]
            if not isinstance(start_step_id, str) or not isinstance(end_step_id, str):
                return JSONResponse(
                    {"ok": False, "error": "局部运行步骤 ID 必须是字符串"},
                    status_code=400,
                )
            if start_step_id and start_step_id not in action_ids:
                return JSONResponse(
                    {"ok": False, "error": "局部运行的起始动作不存在"},
                    status_code=400,
                )
            if end_step_id and end_step_id not in action_ids:
                return JSONResponse(
                    {"ok": False, "error": "局部运行的结束动作不存在"},
                    status_code=400,
                )
            start_index = action_ids.index(start_step_id) if start_step_id else 0
            end_index = action_ids.index(end_step_id) if end_step_id else len(actions) - 1
            if actions and start_index > end_index:
                return JSONResponse(
                    {"ok": False, "error": "局部运行的起始动作不能晚于结束动作"},
                    status_code=400,
                )
            selected_actions = actions[start_index : end_index + 1] if actions else []
            selected_ids = set(action_ids[start_index : end_index + 1])
            skipped_upstream_ids = set(action_ids[:start_index])

            if not isinstance(step_outputs, dict):
                return JSONResponse(
                    {"ok": False, "error": "上游动作结果必须是 JSON 对象"},
                    status_code=400,
                )
            invalid_step_output_ids = sorted(
                key for key in step_outputs
                if not isinstance(key, str) or key not in skipped_upstream_ids
            )
            if invalid_step_output_ids:
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "invalid_test_payload",
                        "error": "上游结果只能对应本次跳过的前置动作",
                        "details": invalid_step_output_ids[:10],
                    },
                    status_code=400,
                )

            execution_source = {
                "preconditions": candidate_rule.get("preconditions", []),
                "actions": selected_actions,
            }
            execution_references = list(iter_references(execution_source))
            required_upstream_ids = sorted({
                node
                for usage in execution_references
                if usage.reference.get("scope") == "step"
                and isinstance(node := usage.reference.get("node"), str)
                and node in skipped_upstream_ids
            })
            missing_upstream_ids = [
                step_id for step_id in required_upstream_ids if step_id not in step_outputs
            ]
            if missing_upstream_ids:
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "missing_test_context",
                        "error": "从中间动作继续时需要提供被跳过的上游结果",
                        "required_step_ids": missing_upstream_ids,
                    },
                    status_code=400,
                )

            upstream_field_issues: List[str] = []
            engine_actions_meta = getattr(engine, "actions_meta", {}) or {}
            actions_by_id = {
                action.get("binding_id"): action
                for action in actions
                if isinstance(action, dict) and isinstance(action.get("binding_id"), str)
            }
            for usage in execution_references:
                reference = usage.reference
                step_id = reference.get("node")
                if reference.get("scope") != "step" or step_id not in skipped_upstream_ids:
                    continue
                current = step_outputs.get(step_id)
                path = reference.get("path", [])
                missing_value = False
                for segment in path if isinstance(path, list) else []:
                    if not isinstance(current, dict) or segment not in current:
                        upstream_field_issues.append(
                            f"{step_id}: 缺少动作输出字段 {'.'.join(path)}"
                        )
                        missing_value = True
                        break
                    current = current[segment]
                if not path or missing_value:
                    continue
                source_action = actions_by_id.get(step_id, {})
                source_meta = engine_actions_meta.get(source_action.get("type", ""), {})
                outputs = source_meta.get("outputs", [])
                output = next(
                    (
                        item for item in outputs
                        if isinstance(item, dict) and item.get("name") == path[0]
                    ),
                    None,
                ) if isinstance(outputs, list) else None
                if output is not None:
                    root_value = step_outputs[step_id].get(path[0])
                    for problem in check_payload_contract(
                        [output], {path[0]: root_value}
                    ):
                        upstream_field_issues.append(f"{step_id}: {problem}")
            if upstream_field_issues:
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "invalid_test_payload",
                        "error": "上游动作结果不符合引用字段契约",
                        "details": upstream_field_issues[:10],
                    },
                    status_code=400,
                )

            if not isinstance(test_assertions, list) or len(test_assertions) > _TEST_ASSERTION_MAX_COUNT:
                return JSONResponse(
                    {"ok": False, "error": "测试断言必须是数组，且不能超过 50 条"},
                    status_code=400,
                )
            normalized_assertions = []
            for assertion_index, assertion in enumerate(test_assertions):
                if not isinstance(assertion, dict):
                    return JSONResponse(
                        {"ok": False, "error": f"测试断言 #{assertion_index + 1} 必须是对象"},
                        status_code=400,
                    )
                step_id = assertion.get("step_id")
                path = assertion.get("path", [])
                operator = assertion.get("operator")
                if (
                    step_id not in selected_ids
                    or operator not in _TEST_ASSERTION_OPERATORS
                    or not isinstance(path, list)
                    or any(
                        not isinstance(segment, str)
                        or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", segment)
                        or segment.startswith("__")
                        for segment in path
                    )
                ):
                    return JSONResponse(
                        {"ok": False, "error": f"测试断言 #{assertion_index + 1} 无效"},
                        status_code=400,
                    )
                normalized = {
                    "step_id": step_id,
                    "path": path,
                    "operator": operator,
                }
                if operator != "exists":
                    if "expected" not in assertion:
                        return JSONResponse(
                            {"ok": False, "error": f"测试断言 #{assertion_index + 1} 缺少期望值"},
                            status_code=400,
                        )
                    normalized["expected"] = assertion["expected"]
                normalized_assertions.append(normalized)

            references = execution_references
            legacy_event_paths = list(iter_legacy_event_payload_paths(execution_source))
            required_trigger_ids = sorted({
                binding_id
                for usage in references
                if usage.reference.get("scope") == "trigger"
                and isinstance(
                    binding_id := usage.reference.get("node"), str
                )
            })
            missing_trigger_ids = [
                binding_id for binding_id in required_trigger_ids
                if not isinstance(trigger_payloads.get(binding_id), dict)
            ]
            needs_event = any(
                usage.reference.get("scope") == "event"
                for usage in references
            ) or bool(legacy_event_paths)
            if missing_trigger_ids or (needs_event and event_payload is None):
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "missing_test_context",
                        "error": "测试规则需要提供触发时产生的数据",
                        "required_trigger_ids": missing_trigger_ids,
                        "event_payload_required": needs_event and event_payload is None,
                    },
                    status_code=400,
                )

            # 先按插件 outputs 检查触发 payload 的字段和类型。
            trigger_field_issues: List[str] = []
            engine_triggers_meta = getattr(engine, "triggers_meta", {}) or {}
            leaves_by_id = {
                leaf.get("binding_id"): leaf
                for leaf in get_rule_events(candidate_rule)
                if isinstance(leaf.get("binding_id"), str)
            }
            unknown_trigger_ids = sorted(set(trigger_payloads) - set(leaves_by_id))
            invalid_trigger_ids = sorted(
                binding_id
                for binding_id, payload in trigger_payloads.items()
                if not isinstance(payload, dict)
            )
            if unknown_trigger_ids or invalid_trigger_ids:
                details = []
                if unknown_trigger_ids:
                    details.append("未知触发器: " + ", ".join(unknown_trigger_ids[:5]))
                if invalid_trigger_ids:
                    details.append("触发数据必须是对象: " + ", ".join(invalid_trigger_ids[:5]))
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "invalid_test_payload",
                        "error": "测试数据包含无效的触发来源",
                        "details": details,
                    },
                    status_code=400,
                )
            for usage in references:
                reference = usage.reference
                if reference.get("scope") != "trigger":
                    continue
                binding_id = reference.get("node")
                if not isinstance(binding_id, str):
                    continue
                payload = trigger_payloads.get(binding_id)
                if not isinstance(payload, dict):
                    continue
                leaf = leaves_by_id.get(binding_id)
                if leaf is None:
                    continue
                trigger_meta = engine_triggers_meta.get(leaf.get("type", ""), {})
                for problem in check_payload_contract(
                    trigger_meta.get("outputs"), payload
                ):
                    trigger_field_issues.append(f"{binding_id}: {problem}")
            if trigger_field_issues:
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "invalid_test_payload",
                        "error": "测试数据不符合触发器输出契约",
                        "details": trigger_field_issues[:10],
                    },
                    status_code=400,
                )

            run_result = engine.run_manual_rule_snapshot(
                candidate_rule,
                rule_index,
                trigger_payloads=trigger_payloads,
                event_payload=event_payload,
                step_outputs=step_outputs,
                start_step_id=start_step_id,
                end_step_id=end_step_id,
                test_assertions=normalized_assertions,
            )
            ok, message = run_result[:2]
            run_id = run_result[2] if len(run_result) > 2 else ""
            if not ok:
                return JSONResponse({"ok": False, "error": message}, status_code=400)
            return {
                "ok": True,
                "message": message,
                "run_id": run_id,
                "action_count": len(selected_actions),
            }

        @app.get("/api/plugins")
        async def plugins_schema():
            return self._get_plugins_schema()

        @app.get("/api/plugins/list")
        async def plugins_list():
            return self._list_all_plugins()

        def _scan_plugin_install_risks(root_path, json_name, meta):
            risks = scan_plugin_security(root_path)
            build = meta.get("build") if isinstance(meta, dict) else None
            if isinstance(build, dict) and (
                build.get("command") or build.get("outputs")
            ):
                risks.append({
                    "id": "build_hook",
                    "label": "安装时执行构建命令",
                    "level": "high",
                    "detail": "点击安装后会以当前用户身份执行插件包声明的 build 命令。",
                    "file": json_name,
                })

            borrowed_findings = []
            for py_file in sorted(Path(root_path).rglob("*.py")):
                if not py_file.is_file():
                    continue
                borrowed_findings.extend(scan_borrowed_privilege(str(py_file)))
            if borrowed_findings:
                risks.append({
                    "id": "borrowed_privilege",
                    "label": "借壳提权嫌疑",
                    "level": "high",
                    "detail": "插件代码可能借其他已授权插件的身份请求管理员权限: "
                    + "；".join(sorted(set(borrowed_findings))[:3]),
                    "file": "*.py",
                })
            return risks

        @app.post("/api/plugins/toggle")
        async def plugin_toggle(request: Request):
            await self._verify_auth(request)
            try:
                body = await request.json()
                ptype = body.get("type", "")
                pid = body.get("id", "")
                if ptype not in ("triggers", "actions") or not pid:
                    return JSONResponse(
                        {"ok": False, "error": "需要 type (triggers/actions) 和 id"},
                        status_code=400)
                return self._toggle_plugin(ptype, pid)
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

        @app.post("/api/plugins/preview")
        async def plugin_preview(request: Request):
            await self._verify_auth(request)
            # 清理超过 30 分钟未安装的预览及其临时目录。
            _now = time.time()
            for _t in [t for t, v in self._pending_previews.items()
                       if _now - v.get("created_at", 0) > 1800]:
                _p = self._pending_previews.pop(_t, None)
                if _p:
                    import shutil as _sh
                    _ed = _p.get("extract_dir")
                    if _ed and os.path.isdir(_ed):
                        _sh.rmtree(_ed, ignore_errors=True)
            import tempfile, py7zr
            from starlette.datastructures import UploadFile
            form = await request.form()
            file = form.get("file")
            if not isinstance(file, UploadFile):
                return JSONResponse({"ok": False, "error": "缺少上传文件"}, status_code=400)
            password = form.get("password", "")
            if not isinstance(password, str):
                password = ""

            data = await file.read()
            if len(data) > _NMFP_MAX_UPLOAD_BYTES:
                return JSONResponse(
                    {"ok": False, "error": f"插件包过大（>{_NMFP_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）"},
                    status_code=400,
                )
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".nmfp")
            extract_dir = None
            try:
                tmp.write(data)
                tmp.close()

                extract_dir = tempfile.mkdtemp()
                _extract_nmfp_safely(tmp.name, extract_dir, password)

                entries = os.listdir(extract_dir)
                dirs = [d for d in entries if os.path.isdir(os.path.join(extract_dir, d))]
                if len(dirs) == 1:
                    root_path = os.path.join(extract_dir, dirs[0])
                elif len(dirs) == 0:
                    root_path = extract_dir
                else:
                    return JSONResponse({"ok": False, "error": "nmfp 根目录应恰好有一个插件文件夹"}, status_code=400)

                has_trigger = os.path.exists(os.path.join(root_path, "trigger.json"))
                has_action = os.path.exists(os.path.join(root_path, "action.json"))
                if has_trigger:
                    ptype, json_name = "triggers", "trigger.json"
                elif has_action:
                    ptype, json_name = "actions", "action.json"
                else:
                    return JSONResponse({"ok": False, "error": "未找到 trigger.json 或 action.json"}, status_code=400)

                try:
                    with open(os.path.join(root_path, json_name), "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except (json.JSONDecodeError, OSError):
                    return JSONResponse({"ok": False, "error": "插件元数据 JSON 损坏或缺失"}, status_code=400)

                plugin_type = "trigger" if ptype == "triggers" else "action"
                ok, errors = validate_plugin_meta(meta, plugin_type)
                schema_valid = ok
                schema_errors = errors[:5] if errors else []

                risks = _scan_plugin_install_risks(root_path, json_name, meta)

                perms = meta.get("permissions", [])
                perm_analysis = []
                for p in perms:
                    info = get_permission_info(p)
                    if info:
                        perm_analysis.append({
                            "permission": p,
                            "label": info["label"],
                            "risk": info["risk"],
                            "description": info["description"],
                            "known": True,
                        })
                    else:
                        perm_analysis.append({
                            "permission": p,
                            "label": p,
                            "risk": "unknown",
                            "description": "未知权限，不在安全规范中",
                            "known": False,
                        })

                perm_conform, perm_errors = check_permissions_conform(perms)

                preview_token = secrets.token_hex(16)
                self._pending_previews[preview_token] = {
                    "extract_dir": extract_dir,
                    "root_path": root_path,
                    "meta": meta,
                    "ptype": ptype,
                    "json_name": json_name,
                    "created_at": time.time(),
                }
                extract_dir = None  # 预览流程接管该目录的清理。

                return {
                    "ok": True,
                    "preview_token": preview_token,
                    "plugin": {
                        "id": meta.get("id", ""),
                        "name": meta.get("name", ""),
                        "description": meta.get("description", ""),
                        "version": meta.get("version", ""),
                        "version_code": meta.get("version_code", 0),
                        "author": meta.get("author", ""),
                        "package_name": meta.get("package_name", ""),
                        "type": ptype,
                        "semantic": meta.get("semantic", ""),
                        "platforms": meta.get("platforms", []),
                        "entrypoints": meta.get("entrypoints", {}),
                        "platform_compatible": (
                            current_platform_name() in meta.get("entrypoints", {})
                            if meta.get("entrypoints")
                            else (
                                not meta.get("platforms")
                                or current_platform_name() in meta.get("platforms", [])
                            )
                        ),
                    },
                    "permissions": perm_analysis,
                    "permission_conform": perm_conform,
                    "permission_errors": perm_errors,
                    "risks": risks,
                    "schema_valid": schema_valid,
                    "schema_errors": schema_errors,
                }
            except ValueError as ve:
                # 解压检查失败时删除临时文件和目录。
                return JSONResponse({"ok": False, "error": str(ve)}, status_code=400)
            finally:
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
                if extract_dir:
                    import shutil
                    shutil.rmtree(extract_dir, ignore_errors=True)

        @app.post("/api/plugins/install")
        async def plugin_install(request: Request):
            await self._verify_auth(request)
            import shutil, py7zr
            from starlette.datastructures import UploadFile
            form = await request.form()

            preview_token = str(form.get("preview_token", "") or "")
            password = str(form.get("password", "") or "")
            if not isinstance(password, str):
                password = ""
            force = str(form.get("force", "")).lower() in ("1", "true", "yes")

            tmp = None

            # 优先从 preview_token 恢复预览目录。
            if preview_token and preview_token in self._pending_previews:
                preview = self._pending_previews.pop(preview_token)
                root_path = preview["root_path"]
                meta = preview["meta"]
                ptype = preview["ptype"]
                json_name = preview["json_name"]
                extract_dir = preview.get("extract_dir")
            else:
                # 没有预览 token 时直接处理上传文件。
                file = form.get("file")
                if not isinstance(file, UploadFile):
                    return JSONResponse({"ok": False, "error": "缺少上传文件或 preview_token 无效"}, status_code=400)

                data = await file.read()
                if len(data) > _NMFP_MAX_UPLOAD_BYTES:
                    return JSONResponse(
                        {"ok": False, "error": f"插件包过大（>{_NMFP_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）"},
                        status_code=400,
                    )
                import tempfile as _tf
                tmp = _tf.NamedTemporaryFile(delete=False, suffix=".nmfp")
                extract_dir = None
                try:
                    tmp.write(data)
                    tmp.close()

                    extract_dir = _tf.mkdtemp()
                    _extract_nmfp_safely(tmp.name, extract_dir, password)

                    entries = os.listdir(extract_dir)
                    dirs = [d for d in entries if os.path.isdir(os.path.join(extract_dir, d))]
                    if len(dirs) == 1:
                        root_path = os.path.join(extract_dir, dirs[0])
                    elif len(dirs) == 0:
                        root_path = extract_dir
                    else:
                        return JSONResponse({"ok": False, "error": "nmfp 根目录应恰好有一个插件文件夹"}, status_code=400)

                    has_trigger = os.path.exists(os.path.join(root_path, "trigger.json"))
                    has_action = os.path.exists(os.path.join(root_path, "action.json"))
                    if has_trigger:
                        ptype, json_name = "triggers", "trigger.json"
                    elif has_action:
                        ptype, json_name = "actions", "action.json"
                    else:
                        return JSONResponse({"ok": False, "error": "未找到 trigger.json 或 action.json"}, status_code=400)

                    try:
                        with open(os.path.join(root_path, json_name), "r", encoding="utf-8") as f:
                            meta = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        return JSONResponse({"ok": False, "error": "插件元数据 JSON 损坏或缺失"}, status_code=400)

                    plugin_type = "trigger" if ptype == "triggers" else "action"
                    ok, errors = validate_plugin_meta(meta, plugin_type)
                    if not ok:
                        return JSONResponse({"ok": False, "error": "schema 校验失败: " + "; ".join(errors[:3])}, status_code=400)
                    risks = _scan_plugin_install_risks(root_path, json_name, meta)
                    if risks:
                        return JSONResponse(
                            {
                                "ok": False,
                                "error": "插件存在安全风险，请先预览后安装",
                                "risks": risks,
                            },
                            status_code=400,
                        )

                except ValueError as ve:
                    # 解压检查失败时删除临时文件和目录。
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                    if extract_dir:
                        shutil.rmtree(extract_dir, ignore_errors=True)
                    return JSONResponse({"ok": False, "error": str(ve)}, status_code=400)
                except Exception:
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                    if extract_dir:
                        shutil.rmtree(extract_dir, ignore_errors=True)
                    raise

            try:
                try:
                    _run_plugin_build_hook(root_path, meta)
                except ValueError as build_error:
                    return JSONResponse(
                        {"ok": False, "error": str(build_error)},
                        status_code=400,
                    )
                pkg = meta.get("package_name", "")
                new_vc = meta.get("version_code", 0)
                pid = meta.get("id", os.path.basename(root_path) if root_path else "")
                user_dir = self._get_user_plugins_dir()

                if not self._is_safe_plugin_id(pid):
                    return JSONResponse(
                        {"ok": False, "error": "插件 id 含非法字符（禁止路径分隔符）"},
                        status_code=400,
                    )

                ptype_dir = os.path.join(user_dir, ptype)
                dest = os.path.join(user_dir, ptype, pid)
                if not os.path.normpath(dest).startswith(
                    os.path.normpath(ptype_dir) + os.sep
                ):
                    return JSONResponse(
                        {"ok": False, "error": "插件路径越界"},
                        status_code=400,
                    )

                perms = meta.get("permissions", [])
                perm_conform, _ = check_permissions_conform(perms)
                sec_mode = detect_security_mode()
                if sec_mode == SecurityMode.STRICT and not perm_conform:
                    unknown = [p for p in perms if not is_known_permission(p)]
                    return JSONResponse(
                        {"ok": False, "error": f"严格模式下拒绝安装：插件请求了未知权限: {', '.join(unknown)}"},
                        status_code=400,
                    )

                existing = self._find_plugin_by_package(pkg)
                if existing:
                    ex_ptype, ex_pid, ex_meta = existing
                    ex_vc = ex_meta.get("version_code", 0)
                    # 同版本重装覆盖现有目录，只阻止版本降级。
                    if not force and new_vc < ex_vc:
                        return JSONResponse(
                            {"ok": False,
                             "error": f"已安装更高版本 v{ex_vc}，如需降级请勾选「强制覆盖」后重试"},
                            status_code=400)
                    shutil.rmtree(os.path.join(user_dir, ex_ptype, ex_pid), ignore_errors=True)

                dest = os.path.join(user_dir, ptype, pid)

                # 用户插件由作者用自己的密钥签名并随包携带 public_key.pem，引擎不再代签

                if os.path.exists(dest):
                    shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(
                    root_path, dest,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
                )

                return {"ok": True, "id": pid, "type": ptype, "package_name": pkg,
                        "version_code": new_vc, "restart_required": True}
            finally:
                if not preview_token and extract_dir:
                    if tmp is not None:
                        try:
                            os.unlink(tmp.name)
                        except Exception:
                            pass
                    shutil.rmtree(extract_dir, ignore_errors=True)
                elif preview_token:
                    # extract_dir 是预览创建的临时目录，两种包布局都应删除它本身。
                    if extract_dir:
                        import shutil as _sh_clean
                        _sh_clean.rmtree(extract_dir, ignore_errors=True)

        @app.get("/api/plugins/key-status")
        async def plugin_key_status():
            priv = str(_PRIVATE_DIR / "signing_private_key.pem")
            if not os.path.exists(priv):
                return {"exists": False, "encrypted": False}
            with open(priv, "rb") as f:
                header = f.read(20)
            encrypted = header.startswith(b"-----BEGIN ENCRYPTED")
            return {"exists": True, "encrypted": encrypted}

        @app.delete("/api/plugins/{ptype}/{pid}")
        async def plugin_uninstall(ptype: str, pid: str, request: Request):
            await self._verify_auth(request)
            if ptype not in ("triggers", "actions"):
                return JSONResponse({"ok": False, "error": "type 必须为 triggers 或 actions"}, status_code=400)
            return self._uninstall_plugin(ptype, pid)

        # 摘要中标记高风险动作类型。
        _HIGH_RISK_ACTIONS = ("run_powershell", "shutdown_system", "kill_process")

        def _config_security_snapshot() -> Dict[str, Any]:
            """读取磁盘上的设置和规则，判定完整性并生成供用户核对的摘要"""
            import notmyfault.config as cfg_mod
            status: Dict[str, Any] = {"status": "ok", "reason": "", "summary": None}
            has_secret = cfg_mod._is_secret_installed()
            if not has_secret:
                status["status"] = "tampered"
                status["reason"] = (
                    "配置签名密钥缺失，无法验证配置是否被篡改。"
                    "引擎已暂停，请核对下方配置摘要后选择处理方式。"
                )

            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config_raw = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                return {"status": "unreadable",
                        "reason": f"配置文件无法读取: {e}", "summary": None}
            if not isinstance(config_raw, dict):
                return {"status": "unreadable",
                        "reason": "配置根节点不是对象", "summary": None}

            config_signature = config_raw.pop("_signature", "")
            if has_secret and (
                not config_signature
                or not cfg_mod._verify_config(config_raw, config_signature)
            ):
                status["status"] = "tampered"
                status["reason"] = (
                    "配置签名校验失败，文件可能被篡改。"
                    "引擎已暂停，请核对下方配置摘要后选择处理方式。"
                )

            # 规则摘要从 rules.json 取，任一文件验签失败都报 tampered
            rules: List[Any] = []
            if os.path.exists(RULES_FILE):
                try:
                    with open(RULES_FILE, "r", encoding="utf-8") as f:
                        rules_raw = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    return {"status": "unreadable",
                            "reason": f"规则文件无法读取: {e}", "summary": None}
                if not isinstance(rules_raw, dict):
                    return {"status": "unreadable",
                            "reason": "规则文件根节点不是对象", "summary": None}
                rules_signature = rules_raw.pop("_signature", "")
                if has_secret and (
                    not rules_signature
                    or not cfg_mod._verify_config(rules_raw, rules_signature)
                ):
                    status["status"] = "tampered"
                    status["reason"] = (
                        "规则签名校验失败，文件可能被篡改。"
                        "引擎已暂停，请核对下方规则摘要后选择处理方式。"
                    )
                loaded = rules_raw.get("rules", [])
                if isinstance(loaded, list):
                    rules = loaded

            status["summary"] = {
                "rule_count": len(rules),
                "rules": [
                    {
                        "name": rule.get("name", f"规则 #{i + 1}") if isinstance(rule, dict) else f"规则 #{i + 1}",
                        "actions": [
                            {
                                "type": action.get("type", "?") if isinstance(action, dict) else "?",
                                "high_risk": isinstance(action, dict)
                                and action.get("type") in _HIGH_RISK_ACTIONS,
                            }
                            for action in rule.get("actions", [])
                            if isinstance(rule, dict)
                        ],
                    }
                    for i, rule in enumerate(rules)
                ],
            }
            return status

        @app.get("/api/config/security-status")
        async def config_security_status():
            return _config_security_snapshot()

        @app.get("/api/settings/admin-authorization")
        async def admin_authorization_setting():
            config = self._load_config()
            selected = get_admin_authorization_mode(config)
            engine = self._resolve_current_engine()
            effective = getattr(engine, "admin_authorization_mode", None)
            supported = ["per_execution"]
            if os.name == "nt":
                supported.append("engine_start")
            return {
                "mode": selected,
                "effective_mode": effective,
                "supported_modes": supported,
                "restart_required": bool(engine is not None and effective != selected),
            }

        @app.put("/api/settings/admin-authorization")
        async def update_admin_authorization_setting(request: Request):
            try:
                body = await request.json()
            except Exception:
                body = None
            mode = body.get("mode") if isinstance(body, dict) else None
            if mode not in ADMIN_AUTHORIZATION_MODES:
                return JSONResponse(
                    {"ok": False, "error": "管理员授权方式无效"},
                    status_code=400,
                )
            if mode == "engine_start" and os.name != "nt":
                return JSONResponse(
                    {"ok": False, "error": "启动时一次授权目前只支持 Windows"},
                    status_code=400,
                )

            try:
                config = self._load_config_for_update()
            except ConfigValidationError as error:
                return JSONResponse(
                    {"ok": False, "error": f"配置未通过完整性校验: {error}"},
                    status_code=409,
                )
            settings = config.get("settings")
            if not isinstance(settings, dict):
                settings = {}
            else:
                settings = dict(settings)
            settings["admin_authorization_mode"] = mode
            config["settings"] = settings
            if not self._save_config(config):
                return JSONResponse(
                    {"ok": False, "error": "无法保存管理员授权方式"},
                    status_code=500,
                )

            engine = self._resolve_current_engine()
            effective = getattr(engine, "admin_authorization_mode", None)
            return {
                "ok": True,
                "mode": mode,
                "effective_mode": effective,
                "restart_required": bool(engine is not None and effective != mode),
            }

        @app.post("/api/config/security-approve")
        async def config_security_approve(request: Request):
            """用户确认后保留现有设置和规则，对两个文件分别重建签名。"""
            await self._verify_auth(request)
            import notmyfault.config as cfg_mod
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    return JSONResponse({"ok": False, "error": "配置根节点不是对象"},
                                        status_code=400)
                raw.pop("_signature", None)
                normalized = cfg_mod._normalize_config(raw)

                # 规则在 rules.json，重签名前再次检查危险命令和结构
                normalized_rules: List[Any] = []
                has_rules_file = os.path.exists(RULES_FILE)
                if has_rules_file:
                    with open(RULES_FILE, "r", encoding="utf-8") as f:
                        rules_raw = json.load(f)
                    if not isinstance(rules_raw, dict):
                        return JSONResponse({"ok": False, "error": "规则文件根节点不是对象"},
                                            status_code=400)
                    rules_raw.pop("_signature", None)
                    rules = rules_raw.get("rules", [])
                    if not isinstance(rules, list):
                        rules = []
                    normalized_rules = cfg_mod._normalize_rules(rules)
                    _warnings, errors = cfg_mod._validate_rules_safety(normalized_rules)
                    if errors:
                        return JSONResponse(
                            {"ok": False,
                             "error": "规则包含危险命令，拒绝重新签名: " + "; ".join(errors[:3])},
                            status_code=400,
                        )

                    structure_errors = validate_rules_structure(normalized_rules)
                    if structure_errors:
                        return JSONResponse(
                            {"ok": False,
                             "error": "规则包含结构无效的规则，拒绝重新签名: "
                             + "; ".join(structure_errors[:3])},
                            status_code=400,
                        )

                if not cfg_mod.save_config(normalized):
                    return JSONResponse({"ok": False, "error": "重新签名失败"},
                                        status_code=500)
                if has_rules_file and not cfg_mod.save_rules(normalized_rules):
                    return JSONResponse({"ok": False, "error": "规则重新签名失败"},
                                        status_code=500)
                return {"ok": True, "message": "配置已重新签名，引擎可正常启动"}
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

        @app.get("/api/engine/diagnostics")
        async def engine_diagnostics():
            engine = self._resolve_current_engine()
            if engine is not None:
                return engine.get_diagnostics()
            return {"uptime_seconds": 0, "plugins": {}, "rules": {}, "actions": {}}

        @app.get("/api/runs")
        async def runs_list(limit: int = 100):
            return {"runs": self._run_history.list_runs(limit)}

        @app.get("/api/runs/{run_id}")
        async def run_detail(run_id: str):
            run = self._run_history.get_run(run_id)
            if run is None:
                return JSONResponse({"detail": "运行记录不存在"}, status_code=404)
            return run

        @app.post("/api/runs/{run_id}/cancel")
        async def run_cancel(run_id: str, request: Request):
            await self._verify_auth(request)
            engine = self._resolve_current_engine()
            if engine is None or not engine.cancel_run(run_id):
                return JSONResponse(
                    {"ok": False, "error": "这次运行已经结束或不存在"},
                    status_code=404,
                )
            return {"ok": True, "message": "已请求停止这次运行"}

        @app.get("/api/plugins/extensions")
        async def plugins_extensions_list():
            engine = self._resolve_current_engine()
            if engine is None:
                return {
                    "commands": [],
                    "parameter_editors": [],
                    "views": [],
                    "data_types": [],
                }
            return engine.extensions.public_contributions()

        @app.post(
            "/api/plugins/{plugin_id}/extensions/commands/{command_id}/invoke"
        )
        async def plugin_extension_command_invoke(
            plugin_id: str,
            command_id: str,
            request: Request,
        ):
            engine = self._resolve_current_engine()
            if engine is None:
                return JSONResponse(
                    {"ok": False, "error": "自动化引擎未运行，无法调用插件扩展"},
                    status_code=409,
                )
            raw_body = await request.body()
            if len(raw_body) > _EXTENSION_MESSAGE_MAX_BYTES:
                return JSONResponse(
                    {"ok": False, "error": "扩展请求不能超过 1 MiB"},
                    status_code=413,
                )
            try:
                body = json.loads(raw_body) if raw_body else {}
            except (TypeError, ValueError):
                body = {}
            if not isinstance(body, dict):
                return JSONResponse(
                    {"ok": False, "error": "扩展请求必须是 JSON 对象"},
                    status_code=400,
                )

            session_id = body.get("session_id")
            session = None
            if isinstance(session_id, str) and session_id:
                session = self._extension_sessions.get(session_id)
                if session is None:
                    return JSONResponse(
                        {"ok": False, "error": "扩展会话不存在或已过期"},
                        status_code=404,
                    )
                if session.plugin_id != plugin_id:
                    return JSONResponse(
                        {"ok": False, "error": "扩展会话不属于该插件"},
                        status_code=400,
                    )
                current_plugin = engine.extensions.plugin(plugin_id)
                if (
                    current_plugin is None
                    or current_plugin.get("meta") is not session.plugin_meta
                ):
                    self._extension_sessions.drop(session.session_id)
                    return JSONResponse(
                        {"ok": False, "error": "插件已经重新加载，请重新打开编辑器"},
                        status_code=409,
                    )
                if command_id not in session.allowed_commands:
                    return JSONResponse(
                        {"ok": False, "error": "当前视图不能调用这个命令"},
                        status_code=403,
                    )
            else:
                source_kind = body.get("source_kind")
                source_id = body.get("source_id")
                if not isinstance(source_kind, str) or not isinstance(source_id, str):
                    return JSONResponse(
                        {"ok": False, "error": "缺少扩展入口信息"},
                        status_code=400,
                    )
                allowed_commands = engine.extensions.source_commands(
                    plugin_id, source_kind, source_id
                )
                if allowed_commands is None:
                    return JSONResponse(
                        {"ok": False, "error": "扩展入口不存在"},
                        status_code=404,
                    )
                if command_id not in allowed_commands:
                    return JSONResponse(
                        {"ok": False, "error": "该入口没有声明这个命令"},
                        status_code=403,
                    )
                source = engine.extensions.contribution(
                    plugin_id, source_kind, source_id
                )
                data_type = engine.extensions.data_type(
                    plugin_id, source.get("data_type", "") if source else ""
                )
                plugin = engine.extensions.plugin(plugin_id)
                if source is None or data_type is None or plugin is None:
                    return JSONResponse(
                        {"ok": False, "error": "扩展入口的数据类型不可用"},
                        status_code=400,
                    )
                current_value = body.get("current_value")
                if current_value is not None:
                    try:
                        if owned_value_identity(current_value) is not None:
                            current_value = unpack_owned_value(
                                current_value,
                                plugin["meta"]["package_name"],
                                data_type["id"],
                                data_type["version"],
                            )
                        elif isinstance(current_value, dict) and "$type" in current_value:
                            raise OwnedValueError("插件数据的归属信息无效")
                        elif source.get("accepts_legacy") is not True:
                            raise OwnedValueError("当前值不是该插件声明的数据")
                    except OwnedValueError as exc:
                        return JSONResponse(
                            {"ok": False, "error": str(exc)}, status_code=400
                        )
                session = self._extension_sessions.create(
                    plugin_id=plugin_id,
                    command_id=command_id,
                    plugin_meta=plugin["meta"],
                    source_kind=source_kind,
                    source_id=source_id,
                    allowed_commands=allowed_commands,
                    data_type=data_type,
                    current_value=current_value,
                )

            handler = engine.extension_handler(plugin_id, command_id)
            if handler is None:
                self._extension_sessions.drop(session.session_id)
                return JSONResponse(
                    {"ok": False, "error": f"插件命令不可用: {command_id}"},
                    status_code=404,
                )
            context = ExtensionContext(session, engine.extensions)
            try:
                result = await asyncio.to_thread(
                    session.invoke, handler, context, body.get("payload")
                )
            except Exception as exc:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"插件命令调用失败: {exc}",
                        "session_id": session.session_id,
                    },
                    status_code=400,
                )
            if result is None:
                result = {"ok": True}
            if not isinstance(result, dict):
                result = {"ok": True, "data": result}
            if result.get("ok") is False:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": result.get("error", "插件命令调用失败"),
                        "session_id": session.session_id,
                    },
                    status_code=400,
                )
            response = {
                "ok": True,
                "session_id": session.session_id,
                "data": result.get("data"),
                "close": result.get("close") is True,
            }
            for key in ("view", "state", "value"):
                if key in result:
                    response[key] = result[key]
            if session.status:
                response["status"] = session.status
            try:
                response_size = len(
                    json.dumps(response, ensure_ascii=False).encode("utf-8")
                )
            except (TypeError, ValueError):
                return JSONResponse(
                    {"ok": False, "error": "插件命令返回了无法保存为 JSON 的数据"},
                    status_code=400,
                )
            if response_size > _EXTENSION_MESSAGE_MAX_BYTES:
                return JSONResponse(
                    {"ok": False, "error": "插件命令返回数据不能超过 1 MiB"},
                    status_code=413,
                )
            if response["close"]:
                self._extension_sessions.drop(session.session_id)
            return response

        @app.delete(
            "/api/plugins/{plugin_id}/extensions/sessions/{session_id}"
        )
        async def plugin_extension_session_close(plugin_id: str, session_id: str):
            session = self._extension_sessions.get(session_id)
            if session is None:
                return {"ok": True}
            if session.plugin_id != plugin_id:
                return JSONResponse(
                    {"ok": False, "error": "扩展会话不属于该插件"},
                    status_code=400,
                )
            self._extension_sessions.drop(session_id)
            return {"ok": True}

        @app.get("/api/plugins/{plugin_id}/extensions/views/{view_id}/page")
        async def plugin_extension_view_page(plugin_id: str, view_id: str):
            engine = self._resolve_current_engine()
            if engine is None:
                return JSONResponse(
                    {"ok": False, "error": "自动化引擎未运行，无法读取插件视图"},
                    status_code=409,
                )
            page = engine.extensions.view_page_path(plugin_id, view_id)
            if page is None:
                return JSONResponse(
                    {"ok": False, "error": "插件视图不存在"}, status_code=404
                )
            try:
                with open(page, "rb") as fp:
                    raw_html = fp.read(_EXTENSION_MESSAGE_MAX_BYTES + 1)
                if len(raw_html) > _EXTENSION_MESSAGE_MAX_BYTES:
                    return JSONResponse(
                        {"ok": False, "error": "插件视图不能超过 1 MiB"},
                        status_code=413,
                    )
                html = raw_html.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                return JSONResponse(
                    {"ok": False, "error": "无法读取插件视图"}, status_code=500
                )
            return {"ok": True, "html": html}

        def resolve_component(plugin_id: str, component_id: str):
            engine = self._resolve_current_engine()
            if engine is None:
                return None, None, "自动化引擎未运行，无法调用插件组件"
            module = engine.component(plugin_id, component_id)
            if module is None:
                return None, None, f"插件组件不可用: {plugin_id}/{component_id}"
            return engine, module, ""

        @app.get("/api/plugins/components")
        async def plugins_components_list():
            engine = self._resolve_current_engine()
            if engine is None:
                return {"components": []}
            items = []
            for kind, meta_store in (
                ("actions", engine.actions_meta),
                ("triggers", engine.triggers_meta),
            ):
                for plugin_id, meta in sorted(meta_store.items()):
                    components = meta.get("components")
                    if not isinstance(components, list):
                        continue
                    for component in components:
                        if not isinstance(component, dict):
                            continue
                        component_id = component.get("id", "")
                        items.append({
                            "plugin_id": plugin_id,
                            "kind": kind,
                            "id": component_id,
                            "name": component.get("name", component_id),
                            "description": component.get("description", ""),
                            "api": component.get("api", "component-v1"),
                            "ui": component.get("ui", {}),
                            "available": engine.component(
                                plugin_id, component_id
                            ) is not None,
                        })
            return {"components": items}

        @app.post("/api/plugins/{plugin_id}/components/{component_id}/invoke")
        async def plugin_component_invoke(
            plugin_id: str, component_id: str, request: Request
        ):
            await self._verify_auth(request)
            engine, module, error = resolve_component(plugin_id, component_id)
            if module is None:
                return JSONResponse({"ok": False, "error": error}, status_code=404)
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            method = body.get("method", "")
            if not isinstance(method, str) or not method:
                return JSONResponse(
                    {"ok": False, "error": "缺少 method 字段"}, status_code=400
                )
            payload = body.get("payload")
            session_id = body.get("session_id")
            meta = (
                engine.actions_meta.get(plugin_id)
                or engine.triggers_meta.get(plugin_id)
                or {}
            )
            if isinstance(session_id, str) and session_id:
                session = self._component_sessions.get(session_id)
                if session is None:
                    return JSONResponse(
                        {"ok": False, "error": "组件会话不存在或已过期"},
                        status_code=404,
                    )
                if (
                    session.plugin_id != plugin_id
                    or session.component_id != component_id
                ):
                    return JSONResponse(
                        {"ok": False, "error": "会话不属于该组件"},
                        status_code=400,
                    )
            else:
                session = self._component_sessions.create(
                    plugin_id, component_id, meta
                )
            try:
                result = await asyncio.to_thread(
                    module.invoke, session, method, payload
                )
            except Exception as exc:
                self._component_sessions.drop(session.session_id)
                return JSONResponse(
                    {"ok": False, "error": f"组件调用失败: {exc}"},
                    status_code=400,
                )
            if result is None:
                result = {}
            if not isinstance(result, dict):
                result = {"data": result}
            if result.get("ok") is False:
                self._component_sessions.drop(session.session_id)
                return JSONResponse(
                    {
                        "ok": False,
                        "error": result.get("error", "组件调用失败"),
                    },
                    status_code=400,
                )
            if result.get("close") is True:
                self._component_sessions.drop(session.session_id)
            response = {
                "ok": True,
                "session_id": session.session_id,
                "data": result,
            }
            if session.status:
                response["status"] = session.status
            return response

        @app.post("/api/desktop-elements/capture")
        async def desktop_element_capture(request: Request):
            await self._verify_auth(request)
            try:
                body = await request.json()
            except Exception:
                body = {}
            try:
                delay = float(body.get("delay_seconds", 3) or 3)
            except (TypeError, ValueError):
                delay = 3.0
            delay = min(max(delay, 1.0), 10.0)
            await asyncio.sleep(delay)
            from notmyfault.native.uia import (
                DesktopElementError,
                capture_element_under_cursor,
            )

            try:
                selector = await asyncio.to_thread(
                    capture_element_under_cursor
                )
            except DesktopElementError as exc:
                return JSONResponse(
                    {"ok": False, "code": exc.code, "error": str(exc)},
                    status_code=400,
                )
            return {"ok": True, "selector": selector}

        @app.post("/api/desktop-elements/check")
        async def desktop_element_check(request: Request):
            await self._verify_auth(request)
            try:
                body = await request.json()
            except Exception:
                body = {}
            from notmyfault.native.uia import DesktopElementError, check_selector

            try:
                result = await asyncio.to_thread(
                    check_selector, body.get("selector")
                )
            except DesktopElementError as exc:
                return JSONResponse(
                    {"ok": False, "code": exc.code, "error": str(exc)},
                    status_code=400,
                )
            return result

        @app.get("/api/engine/logs")
        async def engine_logs(lines: int = 200):
            from notmyfault.core.logging import get_latest_log
            log_path = get_latest_log(
                os.path.join(os.path.dirname(CONFIG_FILE), "logs")
            )
            if not log_path:
                return {"lines": [], "total": 0}
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                return {
                    "lines": [l.rstrip("\n") for l in all_lines[-lines:]],
                    "total": len(all_lines),
                }
            except FileNotFoundError:
                return {"lines": [], "total": 0}

        @app.get("/api/events")
        async def event_stream(request: Request):
            async def generate():
                import asyncio
                client_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
                self._loop = asyncio.get_running_loop()
                with self._sub_lock:
                    self._subscribers.append(client_queue)

                try:
                    while True:
                        if await request.is_disconnected():
                            break

                        try:
                            event = await asyncio.wait_for(client_queue.get(), timeout=15)
                            yield f"event: {event['type']}\n"
                            yield f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"
                finally:
                    with self._sub_lock:
                        if client_queue in self._subscribers:
                            self._subscribers.remove(client_queue)

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # nginx 立即转发 SSE 数据。
                },
            )

    def serve(
        self,
        host: str = "127.0.0.1",
        port: int = 19198,
        sockets: list | None = None,
    ):
        """阻塞当前线程启动 HTTP 服务，端口冲突时报告错误。"""
        print(f"\n{'=' * 50}")
        print(f"  NotmyFault API Server")
        print(f"  监听 http://{host}:{port}")
        print(f"  API 文档: http://{host}:{port}/docs")
        print(f"{'=' * 50}\n")

        config = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            # 关闭 access log，SSE query token 不写入日志。
            access_log=False,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        try:
            self._server.run(sockets=sockets)
        except OSError as e:
            code = getattr(e, 'winerror', None)
            if str(code) == "10048" or "10048" in str(e) or "bind" in str(e).lower():
                print(f"\n[提示] 端口 {port} 已被占用 — 引擎可能已在运行")
            else:
                raise
        except SystemExit:
            # uvicorn 用 SystemExit 完成正常关闭。
            print(f"\n[API] HTTP 服务已退出")
