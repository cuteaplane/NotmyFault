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
from notmyfault.config import (
    CONFIG_FILE,
    ensure_rule_binding_ids,
    save_config as config_save,
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
        zf.extractall(extract_dir)


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
        # 旧测试宿主可注入实例，正式运行时从 EngineRunner.current_engine 读取。
        self._engine_ref = None

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

            for pid in disabled_set:
                if pid in result[ptype]:
                    result[ptype][pid]["enabled"] = False

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

        user_json = os.path.join(user_dir, ptype, pid, json_name)
        if os.path.exists(user_json):
            try:
                with open(user_json, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                meta["enabled"] = not meta.get("enabled", True)
                os.makedirs(os.path.dirname(user_json), exist_ok=True)
                with open(user_json, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                return {"ok": True, "enabled": meta["enabled"],
                        "origin": "user",
                        "restart_required": True}
            except (json.JSONDecodeError, OSError) as e:
                return {"ok": False, "error": str(e)}

        builtin_json = os.path.join(base, ptype, pid, json_name)
        if os.path.exists(builtin_json):
            try:
                config = self._load_config()
                disabled = config.get("disabled_plugins", {})
                if not isinstance(disabled, dict):
                    disabled = {"triggers": [], "actions": []}
                disabled_list = disabled.get(ptype, [])
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
                        "origin": "builtin",
                        "restart_required": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        return {"ok": False, "error": "插件不存在"}

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
            config = self._load_config()
            rules = config.get("rules", [])
            if not isinstance(rules, list):
                rules = []
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
            config = self._load_config()
            rules = config.get("rules", [])
            return {"rules": rules if isinstance(rules, list) else []}

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
            normalized_rules = [
                ensure_rule_binding_ids(rule) for rule in rules
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

            existing_config = self._load_config()
            new_config = dict(existing_config) if isinstance(existing_config, dict) else {}
            new_config.pop("_signature", None)
            new_config["rules"] = normalized_rules
            new_config.setdefault(
                "disabled_plugins",
                {"triggers": [], "actions": []},
            )

            ok = self._save_config(new_config)
            if ok:
                print(f"[API] 规则已保存 ({len(new_config['rules'])} 条)")
                return {"ok": True, "rules": normalized_rules}
            else:
                return JSONResponse(
                    {"ok": False, "error": "写入配置文件失败"},
                    status_code=500,
                )

        @app.post("/api/rules/{rule_index}/run")
        async def rules_run(rule_index: int, request: Request):
            engine = self._resolve_current_engine()
            if engine is None:
                return JSONResponse(
                    {"ok": False, "error": "引擎尚未就绪"}, status_code=409,
                )
            rule_snapshot = None
            has_snapshot = False
            trigger_payloads: Dict[str, Dict[str, Any]] = {}
            event_payload = None
            raw_body = await request.body()
            if raw_body:
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

            candidate_rule = None
            if has_snapshot:
                structure_errors = validate_rules_structure([rule_snapshot])
                if structure_errors:
                    return JSONResponse(
                        {"ok": False, "error": "规则结构校验失败",
                         "details": structure_errors[:10]},
                        status_code=400,
                    )
                disk_rules = self._load_config().get("rules", [])
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
                disk_rules = self._load_config().get("rules", [])
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

            references = list(iter_references(candidate_rule))
            legacy_event_paths = list(iter_legacy_event_payload_paths(candidate_rule))
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
            assert isinstance(candidate_rule, dict)
            engine_triggers_meta = getattr(engine, "triggers_meta", {}) or {}
            leaves_by_id = {
                leaf.get("binding_id"): leaf
                for leaf in get_rule_events(candidate_rule)
                if isinstance(leaf.get("binding_id"), str)
            }
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

            if trigger_payloads or event_payload is not None:
                ok, message = engine.run_manual_rule_snapshot(
                    candidate_rule,
                    rule_index,
                    trigger_payloads=trigger_payloads,
                    event_payload=event_payload,
                )
            else:
                ok, message = engine.run_manual_rule_snapshot(
                    candidate_rule,
                    rule_index,
                )
            if not ok:
                return JSONResponse({"ok": False, "error": message}, status_code=400)
            return {"ok": True, "message": message}

        @app.get("/api/plugins")
        async def plugins_schema():
            return self._get_plugins_schema()

        @app.get("/api/plugins/list")
        async def plugins_list():
            return self._list_all_plugins()

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

                risks = scan_plugin_security(root_path)

                # 扫描导入或调用其他插件模块的代码。
                borrowed_findings = []
                for py_file in sorted(Path(root_path).rglob("*.py")):
                    if not py_file.is_file():
                        continue
                    borrowed_findings.extend(
                        scan_borrowed_privilege(str(py_file))
                    )
                if borrowed_findings:
                    risks.append({
                        "id": "borrowed_privilege",
                        "label": "借壳提权嫌疑",
                        "level": "high",
                        "detail": "插件代码可能借其他已授权插件的身份请求管理员权限: "
                        + "；".join(sorted(set(borrowed_findings))[:3]),
                        "file": "*.py",
                    })

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

                priv_key_path = str(_PRIVATE_DIR / "signing_private_key.pem")
                if not os.path.exists(priv_key_path):
                    return JSONResponse({"ok": False, "error": "私钥不存在，请先运行 build.py init-keys"}, status_code=400)

                try:
                    from notmyfault.security.signing import sign_plugin, load_private_key
                    from pathlib import Path
                    pk = load_private_key(Path(priv_key_path), password=password or None)
                    sign_plugin(Path(root_path), json_name, pk)
                except Exception as e:
                    err = str(e)
                    if "password" in err.lower() or "bad decrypt" in err.lower():
                        return JSONResponse({"ok": False, "error": "私钥密码错误"}, status_code=400)
                    return JSONResponse({"ok": False, "error": "签名失败: " + err}, status_code=400)

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
            """读取磁盘配置，判定完整性并生成供用户核对的摘要"""
            import notmyfault.config as cfg_mod
            status: Dict[str, Any] = {"status": "ok", "reason": "", "summary": None}
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                return {"status": "unreadable",
                        "reason": f"配置文件无法读取: {e}", "summary": None}
            if not isinstance(raw, dict):
                return {"status": "unreadable",
                        "reason": "配置根节点不是对象", "summary": None}

            signature = raw.pop("_signature", "")
            has_secret = cfg_mod._is_secret_installed()
            if not has_secret:
                status["status"] = "tampered"
                status["reason"] = (
                    "配置签名密钥缺失，无法验证配置是否被篡改。"
                    "引擎已暂停，请核对下方配置摘要后选择处理方式。"
                )
            elif not signature or not cfg_mod._verify_config(raw, signature):
                status["status"] = "tampered"
                status["reason"] = (
                    "配置签名校验失败，文件可能被篡改。"
                    "引擎已暂停，请核对下方配置摘要后选择处理方式。"
                )

            rules = raw.get("rules", [])
            if not isinstance(rules, list):
                rules = []
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

        @app.post("/api/config/security-approve")
        async def config_security_approve(request: Request):
            """用户确认配置后保留现有规则，重建签名并恢复引擎运行。"""
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
                # 重签名前再次检查危险命令。
                rules = normalized.get("rules", [])
                if not isinstance(rules, list):
                    rules = []
                _warnings, errors = cfg_mod._validate_rules_safety(rules)
                if errors:
                    return JSONResponse(
                        {"ok": False,
                         "error": "配置包含危险规则，拒绝重新签名: " + "; ".join(errors[:3])},
                        status_code=400,
                    )

                # 重签名前还要检查规则结构和字段类型。
                structure_errors = validate_rules_structure(rules)
                if structure_errors:
                    return JSONResponse(
                        {"ok": False,
                         "error": "配置包含结构无效的规则，拒绝重新签名: "
                         + "; ".join(structure_errors[:3])},
                        status_code=400,
                    )
                ok = cfg_mod.save_config(normalized)
                if not ok:
                    return JSONResponse({"ok": False, "error": "重新签名失败"},
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
