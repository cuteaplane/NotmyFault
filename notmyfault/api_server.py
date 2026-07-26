"""
NotmyFault HTTP API 服务器
——————————————————————————
本地 REST API + SSE 事件流，替代w***d的 IPC 层。

引擎启动后，UI 通过 HTTP 请求控制引擎，通过 SSE 接收实时事件。
Dashboard 仅通过 pywebview 桌面桥接访问；不提供浏览器管理模式。
"""

import json
import secrets
import os
import sys
import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Dict, Protocol

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

from notmyfault.config import CONFIG_FILE, save_config as config_save
from notmyfault.plugin_schema import (
    scan_plugins,
    validate_plugin_meta,
    scan_plugin_security,
    check_permissions_conform,
    get_permission_info,
    is_known_permission,
    PERMISSION_REGISTRY,
)
from notmyfault.security import detect_security_mode, SecurityMode
from notmyfault.rules import get_rule_events, validate_rules_structure
from notmyfault.version import __version__

_scan_plugins = scan_plugins  # 向后兼容

# API 认证令牌。令牌文件是 Dashboard 与后台服务之间的本机凭据，
# 在权限仍安全的前提下跨后台服务重启复用，避免两进程生命周期不同步时失联。
API_TOKEN: str = ""
# token 文件存放在 %APPDATA%/NotmyFault/ 下（与 config.json 同目录），
# 不再使用 %TEMP%——TEMP 目录默认 Authenticated Users 可读，权限过宽。
API_TOKEN_FILE: str = os.path.join(os.path.dirname(CONFIG_FILE), ".api_token")
# dashboard.pyw 在端口被占用时会从 19199 起顺延；所有候选地址都只绑定
# loopback，仍然是本机 pywebview 的受信任来源。若只允许 19199，第二次唤醒
# 或旧 WebView 残留占端口时，CORSMiddleware 会让 OPTIONS 直接返回 400，
# 前端便会把仍在运行的引擎误判为离线。
_DASHBOARD_ORIGINS = [
    f"http://{host}:{port}"
    for host in ("127.0.0.1", "localhost")
    for port in range(19199, 19219)
]


def _secure_write_token(path: str, token: str) -> None:
    """安全写入 token 文件，限制权限仅当前用户可访问。

    - 用 os.open 创建文件并设置 0o600（Unix 生效；Windows 上部分生效）
    - Windows 上额外用 icacls 移除继承权限，仅保留当前用户 Full control
      （os.getlogin() 在某些环境下返回的用户名不被 icacls 识别，改用 %USERNAME%）
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    try:
        # 先写到临时文件再原子替换，避免半写状态
        tmp_path = path + ".tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.replace(tmp_path, path)
        # Windows 上用 icacls 限制 DACL：移除继承，仅当前用户 Full control
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
    """读取现有安全 token；文件缺失或内容损坏时才生成新 token。"""
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



# ---------------------------------------------------------------------------
# 引擎运行器接口 (由 NOTMYFAULT.pyw 的 EngineRunner 实现)
# ---------------------------------------------------------------------------

class EngineRunnerLike(Protocol):
    """api_server 期望的引擎运行器接口"""
    engine_running: bool
    shutdown_event: Any  # threading.Event
    engine_thread: Any   # threading.Thread | None

    def _start_engine_core(self) -> bool: ...
    def _stop_engine(self) -> bool: ...
    def _request_process_shutdown(self, force_after: float = 10) -> None: ...


# ---------------------------------------------------------------------------
# EngineAPI
# ---------------------------------------------------------------------------

class EngineAPI:
    """
    引擎 HTTP API 服务

    职责：
    1. 暴露 REST endpoint 让 UI 控制引擎
    2. 提供 SSE 事件流，实时推送引擎事件到 UI
    3. 线程安全的事件队列，桥接引擎线程和 asyncio 事件循环
    """

    def __init__(self, engine_runner: EngineRunnerLike):
        self._engine = engine_runner
        self._subscribers: list["asyncio.Queue"] = []

        self._loop = None
        self._sub_lock = threading.Lock()
        self._server = None
        self._engine_ref = None

        # 插件预览暂存：{token: {"extract_dir", "root_path", "meta", "ptype", "created_at"}}
        self._pending_previews: Dict[str, Any] = {}

        self.app = FastAPI(title="NotmyFault Engine API", version=__version__)
        self._setup_middleware()
        self._setup_routes()
        global API_TOKEN
        API_TOKEN = _load_or_create_api_token(API_TOKEN_FILE)

    # ---- CORS -----------------------------------------------------------

    def _setup_middleware(self):
        self.app.add_middleware(
            CORSMiddleware,
            # Dashboard 是本机静态站点；不能让任意网页跨域读取本地自动化数据。
            allow_origins=_DASHBOARD_ORIGINS,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self.app.middleware("http")(self._auth_middleware)

    async def _auth_middleware(self, request: Request, call_next):
        """为全部 API 路由统一认证，避免新 GET 端点漏掉校验。"""
        if request.url.path.startswith("/api/"):
            try:
                await self._verify_auth(request)
            except HTTPException as exc:
                # BaseHTTPMiddleware 之外抛出的 HTTPException 不会自动转成响应。
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    # ---- 事件推送 (引擎线程 → SSE) ---------------------------------------

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

    # ---- 插件扫描 (供 /api/plugins 使用) --------------------------------

    def _get_plugins_schema(self) -> Dict[str, Any]:
        base = os.path.dirname(__file__)
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
        return os.path.join(os.environ.get("APPDATA", ""), "NotmyFault", "plugins")

    @staticmethod
    def _is_safe_plugin_id(pid: str) -> bool:
        """校验插件 id 不含路径穿越字符（防 ../ 越界）。

        与 plugin_install 端点校验保持一致：禁止路径分隔符和 ..。
        toggle/uninstall 端点之前缺少这层校验，可构造
        `DELETE /api/plugins/triggers/..%2F..%2F..%2Fdir` 删除任意目录。
        """
        if not pid:
            return False
        if "/" in pid or "\\" in pid or ".." in pid:
            return False
        return True

    def _find_plugin_by_package(self, package_name: str):
        """在用户插件目录中按 package_name 查找已安装插件。

        返回 (ptype, pid, meta) 或 None。
        """
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
        base = os.path.dirname(__file__)
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
                # scan_plugins 会跳过 disabled 和 schema 校验失败的插件，
                # 导致这些插件不显示在 dashboard 上，用户无法禁用/卸载。
                # 先用 scan_plugins 获取有效插件，再兜底扫描所有目录，
                # 把被跳过的加回来（与 builtin 插件逻辑一致）。
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
                        # schema 校验失败的插件标注 _error，让用户知道为什么加载失败
                        is_valid, errors = validate_plugin_meta(meta, plugin_type)
                        if not is_valid:
                            meta["_error"] = "schema: " + "; ".join(errors[:2])
                        result[ptype][pid] = meta

        # merge diagnostics (loaded status / errors)
        engine = getattr(self, "_engine_ref", None)
        if engine is not None:
            diag = engine.get_diagnostics()
            for err in diag.get("plugins", {}).get("errors", []):
                # errors: ["Trigger", plugin_id, reason]
                if len(err) >= 3:
                    etype, epid, ereason = err[0], err[1], err[2]
                    cat = "triggers" if etype == "Trigger" else "actions"
                    if epid in result.get(cat, {}):
                        result[cat][epid]["_error"] = ereason

        return result

    def _toggle_plugin(self, ptype: str, pid: str) -> dict:
        if not self._is_safe_plugin_id(pid):
            return {"ok": False, "error": "插件 id 含非法字符（禁止路径分隔符）"}
        base = os.path.dirname(__file__)
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


    # ---- 配置读写辅助 ----------------------------------------------------

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

    # ---- 路由注册 --------------------------------------------------------

    async def _verify_auth(self, request: Request) -> None:
        # CORS 预检没有凭据，必须交给 CORSMiddleware 正常响应。
        if request.method == "OPTIONS":
            return
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
        # 原生 EventSource 不能设置 Authorization；仅 SSE 接口接受 query token。
        if not token and request.url.path == "/api/events":
            token = request.query_params.get("token", "")
        if not token or not secrets.compare_digest(token, API_TOKEN):
            # Dashboard 从磁盘读取 token，而服务端校验内存中的 token。若 token
            # 文件被清理、截断或意外覆盖，两边会永久失联。认证失败时由仍在监听
            # 端口的实例重新发布自己的 token；客户端重读后即可恢复。当前请求仍
            # 返回 403，避免把错误 token 当成已认证。
            self._repair_token_file()
            print(f"[Auth] 403 rejected: has_hdr={bool(auth)} req_len={len(token)} srv_len={len(API_TOKEN)}",
                  file=sys.stderr)
            raise HTTPException(status_code=403, detail="Forbidden: invalid API Token")

    @staticmethod
    def _repair_token_file() -> None:
        """确保磁盘 token 与当前正在提供服务的实例一致。"""
        try:
            with open(API_TOKEN_FILE, "r", encoding="utf-8") as f:
                if secrets.compare_digest(f.read().strip(), API_TOKEN):
                    return
        except OSError:
            pass
        _secure_write_token(API_TOKEN_FILE, API_TOKEN)

    def _setup_routes(self):
        app = self.app

        # ================================================================
        # 引擎控制
        # ================================================================

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

            started = self._engine._start_engine_core()
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
            stopped = self._engine._stop_engine()
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
            """彻底退出引擎进程（先停引擎，再优雅关闭 HTTP 服务）"""
            print("[API] POST /api/engine/shutdown")
            request_shutdown = getattr(
                self._engine,
                "_request_process_shutdown",
                None,
            )
            if callable(request_shutdown):
                request_shutdown()
            else:
                self._engine._stop_engine()
                if self._server:
                    self._server.should_exit = True

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
            }

        # ================================================================
        # 规则 CRUD
        # ================================================================

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

            # 安全校验：命中危险模式（危险命令/危险路径）则拒绝写入
            from notmyfault.config import _validate_rules_safety
            _warnings, errors = _validate_rules_safety(rules)
            if errors:
                return JSONResponse(
                    {"ok": False, "error": "规则安全校验失败", "details": errors[:10]},
                    status_code=400,
                )

            existing_config = self._load_config()
            new_config = dict(existing_config) if isinstance(existing_config, dict) else {}
            new_config.pop("_signature", None)
            new_config["rules"] = rules
            new_config.setdefault(
                "disabled_plugins",
                {"triggers": [], "actions": []},
            )

            ok = self._save_config(new_config)
            if ok:
                print(f"[API] 规则已保存 ({len(new_config['rules'])} 条)")
                return {"ok": True}
            else:
                return JSONResponse(
                    {"ok": False, "error": "写入配置文件失败"},
                    status_code=500,
                )

        @app.post("/api/rules/{rule_index}/run")
        async def rules_run(rule_index: int, request: Request):
            engine = self._engine_ref
            if engine is None:
                return JSONResponse(
                    {"ok": False, "error": "引擎尚未就绪"}, status_code=409,
                )
            rule_snapshot = None
            has_snapshot = False
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
                ok, message = engine.run_manual_rule_snapshot(
                    rule_snapshot,
                    rule_index,
                )
            else:
                ok, message = engine.run_manual_rule(rule_index)
            if not ok:
                return JSONResponse({"ok": False, "error": message}, status_code=400)
            return {"ok": True, "message": message}

        # ================================================================
        # 插件管理
        # ================================================================

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
            # 清理超过 30 分钟未安装的预览（避免临时目录+内存泄漏）
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
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".nmfp")
            extract_dir = None
            try:
                tmp.write(data)
                tmp.close()

                extract_dir = tempfile.mkdtemp()
                with py7zr.SevenZipFile(tmp.name, mode="r", password=password or None) as zf:
                    zf.extractall(extract_dir)

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

                # 安全扫描
                risks = scan_plugin_security(root_path)

                # 权限分析
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

                # 权限合规检查
                perm_conform, perm_errors = check_permissions_conform(perms)

                # 生成预览 token
                preview_token = secrets.token_hex(16)
                self._pending_previews[preview_token] = {
                    "extract_dir": extract_dir,
                    "root_path": root_path,
                    "meta": meta,
                    "ptype": ptype,
                    "json_name": json_name,
                    "created_at": time.time(),
                }
                extract_dir = None  # 防止 finally 清空

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
                    },
                    "permissions": perm_analysis,
                    "permission_conform": perm_conform,
                    "permission_errors": perm_errors,
                    "risks": risks,
                    "schema_valid": schema_valid,
                    "schema_errors": schema_errors,
                }
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

            # 优先从 preview_token 恢复
            if preview_token and preview_token in self._pending_previews:
                preview = self._pending_previews.pop(preview_token)
                root_path = preview["root_path"]
                meta = preview["meta"]
                ptype = preview["ptype"]
                json_name = preview["json_name"]
                extract_dir = preview.get("extract_dir")
                # 清理会在 finally 中完成
            else:
                # 回退：直接上传安装（无预览）
                file = form.get("file")
                if not isinstance(file, UploadFile):
                    return JSONResponse({"ok": False, "error": "缺少上传文件或 preview_token 无效"}, status_code=400)

                data = await file.read()
                import tempfile as _tf
                tmp = _tf.NamedTemporaryFile(delete=False, suffix=".nmfp")
                extract_dir = None
                try:
                    tmp.write(data)
                    tmp.close()

                    extract_dir = _tf.mkdtemp()
                    with py7zr.SevenZipFile(tmp.name, mode="r", password=password or None) as zf:
                        zf.extractall(extract_dir)

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
                except Exception:
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                    if extract_dir:
                        shutil.rmtree(extract_dir, ignore_errors=True)
                    raise

            # ---- 共享安装逻辑 ----
            try:
                pkg = meta.get("package_name", "")
                new_vc = meta.get("version_code", 0)
                pid = meta.get("id", os.path.basename(root_path) if root_path else "")
                user_dir = self._get_user_plugins_dir()

                # 路径穿越防御
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

                # 许可权限校验 vs security_mode
                perms = meta.get("permissions", [])
                perm_conform, _ = check_permissions_conform(perms)
                sec_mode = detect_security_mode()
                if sec_mode == SecurityMode.STRICT and not perm_conform:
                    unknown = [p for p in perms if not is_known_permission(p)]
                    return JSONResponse(
                        {"ok": False, "error": f"严格模式下拒绝安装：插件请求了未知权限: {', '.join(unknown)}"},
                        status_code=400,
                    )

                # 全局包名唯一性检查 + 版本对比
                existing = self._find_plugin_by_package(pkg)
                if existing:
                    ex_ptype, ex_pid, ex_meta = existing
                    ex_vc = ex_meta.get("version_code", 0)
                    # 同版本重装是幂等覆盖：方便修复包和重复安装；只阻止降级。
                    if not force and new_vc < ex_vc:
                        return JSONResponse(
                            {"ok": False,
                             "error": f"已安装更高版本 v{ex_vc}，如需降级请勾选「强制覆盖」后重试"},
                            status_code=400)
                    shutil.rmtree(os.path.join(user_dir, ex_ptype, ex_pid), ignore_errors=True)

                dest = os.path.join(user_dir, ptype, pid)

                # 签名
                priv_key_path = os.path.join(os.path.dirname(__file__), "..", ".private", "signing_private_key.pem")
                if not os.path.exists(priv_key_path):
                    return JSONResponse({"ok": False, "error": "私钥不存在，请先运行 build.py init-keys"}, status_code=400)

                try:
                    from notmyfault.signing import sign_plugin, load_private_key
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
                    # extract_dir 始终是预览时 mkdtemp 的临时解压目录；
                    # 无论 root_path 是 extract_dir 本身（平铺打包）还是其子目录（单文件夹打包），
                    # 删 extract_dir 即可。不可用 dirname(root_path)，否则平铺打包会误删 Temp 父目录。
                    if extract_dir:
                        import shutil as _sh_clean
                        _sh_clean.rmtree(extract_dir, ignore_errors=True)

        @app.get("/api/plugins/key-status")
        async def plugin_key_status():
            priv = os.path.join(os.path.dirname(__file__), "..", ".private", "signing_private_key.pem")
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

        # ================================================================
        # 诊断 & 日志
        # ================================================================

        @app.get("/api/engine/diagnostics")
        async def engine_diagnostics():
            engine = getattr(self, "_engine_ref", None)
            if engine is not None:
                return engine.get_diagnostics()
            return {"uptime_seconds": 0, "plugins": {}, "rules": {}, "actions": {}}

        @app.get("/api/engine/logs")
        async def engine_logs(lines: int = 200):
            from notmyfault.logging import get_latest_log
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

        # ================================================================
        # SSE 事件流
        # ================================================================

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
                    "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
                },
            )

    # ---- 启动 HTTP 服务 --------------------------------------------------

    def serve(
        self,
        host: str = "127.0.0.1",
        port: int = 19198,
        sockets: list | None = None,
    ):
        """启动 HTTP 服务（阻塞当前线程，端口冲突时优雅退出不崩溃）"""
        print(f"\n{'=' * 50}")
        print(f"  NotmyFault API Server")
        print(f"  监听 http://{host}:{port}")
        print(f"  API 文档: http://{host}:{port}/docs")
        print(f"{'=' * 50}\n")

        config = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            log_level="info",
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
            # uvicorn 内部通过 sys.exit() 完成干净关闭，这是预期行为
            print(f"\n[API] HTTP 服务已退出")
