"""
NotmyFault HTTP API 服务器
——————————————————————————
本地 REST API + SSE 事件流，替代w***d的 IPC 层。

引擎启动后，UI 通过 HTTP 请求控制引擎，通过 SSE 接收实时事件。
可以用任何浏览器打开 dashboard.html 直接管理。
"""

import json
import secrets
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Callable, Protocol

import uvicorn
from fastapi import FastAPI, Request, HTTPException, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

from notmyfault.config import CONFIG_FILE, save_config as config_save, save_config as config_save
from notmyfault.plugin_schema import scan_plugins

_scan_plugins = scan_plugins  # 向后兼容

# API 认证令牌（每次进程启动时随机生成）
API_TOKEN: str = secrets.token_hex(32)
API_TOKEN_FILE: str = os.path.join(os.environ.get("TEMP", ""), "notmyfault_api_token")



# ---------------------------------------------------------------------------
# 引擎运行器接口 (由 NOTMYFAULT.pyw 的 EngineRunner 实现)
# ---------------------------------------------------------------------------

class EngineRunnerLike(Protocol):
    """api_server 期望的引擎运行器接口"""
    engine_running: bool
    shutdown_event: Any  # threading.Event
    engine_thread: Any   # threading.Thread | None

    def _start_engine_core(self) -> None: ...
    def _stop_engine(self) -> None: ...


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
        self._subscribers: list[tuple[Any, Any]] = []
        self._sub_lock = threading.Lock()
        self._server = None
        self._engine_ref = None

        self.app = FastAPI(title="NotmyFault Engine API", version="1.0")
        self._setup_middleware()
        self._setup_routes()
        global API_TOKEN
        API_TOKEN = secrets.token_hex(32)
        try:
            os.makedirs(os.path.dirname(API_TOKEN_FILE), exist_ok=True)
        except OSError:
            pass
        try:
            with open(API_TOKEN_FILE, "w") as f:
                f.write(API_TOKEN)
        except OSError:
            pass

    # ---- CORS -----------------------------------------------------------

    def _setup_middleware(self):
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ---- 事件推送 (引擎线程 → SSE) ---------------------------------------

    def push_event(self, event_type: str, data: Dict[str, Any]):
        packet = {"type": event_type, "data": data, "ts": time.time()}
        with self._sub_lock:
            subscribers = list(self._subscribers)

        for loop, q in subscribers:
            def _push(target=q):
                try:
                    target.put_nowait(packet)
                except Exception:
                    pass

            try:
                loop.call_soon_threadsafe(_push)
            except RuntimeError:
                with self._sub_lock:
                    if (loop, q) in self._subscribers:
                        self._subscribers.remove((loop, q))

    # ---- 插件扫描 (供 /api/plugins 使用) --------------------------------

    def _get_plugins_schema(self) -> Dict[str, Any]:
        base = os.path.dirname(__file__)
        return {
            "triggers": scan_plugins(base, "triggers", "trigger.json"),
            "actions": scan_plugins(base, "actions", "action.json"),
        }

    def _get_user_plugins_dir(self) -> str:
        return os.path.join(os.environ.get("APPDATA", ""), "NotmyFault", "plugins")

    def _list_all_plugins(self) -> Dict[str, Any]:
        base = os.path.dirname(__file__)
        user_dir = self._get_user_plugins_dir()
        disabled_plugins = self._load_config().get("disabled_plugins", {})

        result: Dict[str, Dict] = {"triggers": {}, "actions": {}}
        for ptype in ("triggers", "actions"):
            json_name = "trigger.json" if ptype == "triggers" else "action.json"
            # builtin
            for pid, meta in scan_plugins(base, ptype, json_name).items():
                meta["origin"] = meta.get("origin", "builtin")
                if pid in disabled_plugins.get(ptype, []):
                    meta["enabled"] = False
                result[ptype][pid] = meta
            # user
            if os.path.isdir(user_dir):
                for pid, meta in scan_plugins(user_dir, ptype, json_name).items():
                    meta["origin"] = meta.get("origin", "user")
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
        base = os.path.dirname(__file__)
        user_dir = self._get_user_plugins_dir()
        json_name = "trigger.json" if ptype == "triggers" else "action.json"

        for root in (user_dir,):
            json_path = os.path.join(root, ptype, pid, json_name)
            if os.path.exists(json_path):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    meta["enabled"] = not meta.get("enabled", True)
                    os.makedirs(os.path.dirname(json_path), exist_ok=True)
                    with open(json_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)
                    return {"ok": True, "enabled": meta["enabled"],
                            "origin": "user",
                            "restart_required": True}
                except (json.JSONDecodeError, OSError) as e:
                    return {"ok": False, "error": str(e)}

        builtin_json = os.path.join(base, ptype, pid, json_name)
        if not os.path.exists(builtin_json):
            return {"ok": False, "error": "插件不存在"}

        config = self._load_config()
        disabled_plugins = config.setdefault("disabled_plugins", {})
        disabled = set(disabled_plugins.get(ptype, []))
        if pid in disabled:
            disabled.remove(pid)
            enabled = True
        else:
            disabled.add(pid)
            enabled = False
        disabled_plugins[ptype] = sorted(disabled)

        if not self._save_config(config):
            return {"ok": False, "error": "写入配置文件失败"}
        return {"ok": True, "enabled": enabled, "origin": "builtin", "restart_required": True}

    def _uninstall_plugin(self, ptype: str, pid: str) -> dict:
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
        if request.method == "GET":
            return
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
        if not token or not secrets.compare_digest(token, API_TOKEN):
            raise HTTPException(status_code=403, detail="Forbidden: invalid API Token")

    def _setup_routes(self):
        app = self.app

        # ================================================================
        # 引擎控制
        # ================================================================

        @app.post("/api/engine/start")
        async def engine_start(request: Request):
            await self._verify_auth(request)
            print("[API] POST /api/engine/start")
            if self._engine.engine_running:
                return {"ok": True, "running": True, "message": "already_running"}

            self._engine._start_engine_core()
            return {"ok": True, "running": self._engine.engine_running}

        @app.post("/api/engine/stop")
        async def engine_stop(request: Request):
            await self._verify_auth(request)
            print("[API] POST /api/engine/stop")
            self._engine._stop_engine()
            return {"ok": True}

        @app.post("/api/engine/shutdown")
        async def engine_shutdown(request: Request):
            await self._verify_auth(request)
            """彻底退出引擎进程（先停引擎，再优雅关闭 HTTP 服务）"""
            print("[API] POST /api/engine/shutdown")
            self._engine._stop_engine()

            # 触发 uvicorn 优雅关闭 — 替代 os._exit(0)
            if self._server:
                self._server.should_exit = True

            return {"ok": True, "message": "shutting_down"}

        @app.get("/api/engine/status")
        async def engine_status():
            config = self._load_config()
            rules = config.get("rules", [])
            return {
                "running": self._engine.engine_running,
                "engine_running": self._engine.engine_running,
                "api_alive": True,
                "pid": os.getpid(),
                "rules_count": len(rules),
                "triggers_count": len(set(
                    r.get("event", {}).get("type", "")
                    for r in rules
                )),
                "actions_count": len(set(
                    a.get("type", "")
                    for r in rules
                    for a in r.get("actions", [])
                )),
            }

        # ================================================================
        # 规则 CRUD
        # ================================================================

        @app.get("/api/rules")
        async def rules_list():
            config = self._load_config()
            return {"rules": config.get("rules", [])}

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

            new_config = {
                "rules": body.get("rules", []),
            }
            current_config = self._load_config()
            if "disabled_plugins" in current_config:
                new_config["disabled_plugins"] = current_config["disabled_plugins"]

            ok = self._save_config(new_config)
            if ok:
                print(f"[API] 规则已保存 ({len(new_config['rules'])} 条)")
                return {"ok": True}
            else:
                return JSONResponse(
                    {"ok": False, "error": "写入配置文件失败"},
                    status_code=500,
                )

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

        @app.post("/api/plugins/install")
        async def plugin_install(request: Request):
            await self._verify_auth(request)
            import tempfile, zipfile, shutil
            form = await request.form()
            file = form.get("file")
            if not file:
                return JSONResponse({"ok": False, "error": "缺少上传文件"}, status_code=400)

            data = await file.read()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            try:
                tmp.write(data)
                tmp.close()
                with zipfile.ZipFile(tmp.name, "r") as zf:
                    names = zf.namelist()
                    # 找根目录名
                    roots = set(n.split("/")[0] for n in names if "/" in n)
                    if len(roots) != 1:
                        return JSONResponse({"ok": False, "error": "zip 根目录应恰好有一个插件文件夹"}, status_code=400)
                    root_name = roots.pop()

                    # 确定是 trigger 还是 action
                    has_trigger = f"{root_name}/trigger.json" in names
                    has_action = f"{root_name}/action.json" in names
                    if has_trigger:
                        ptype, json_name = "triggers", "trigger.json"
                    elif has_action:
                        ptype, json_name = "actions", "action.json"
                    else:
                        return JSONResponse({"ok": False, "error": "zip 中未找到 trigger.json 或 action.json"}, status_code=400)

                    # 校验 metadata
                    try:
                        meta = json.loads(zf.read(f"{root_name}/{json_name}"))
                    except (json.JSONDecodeError, KeyError):
                        return JSONResponse({"ok": False, "error": "插件元数据 JSON 损坏或缺失"}, status_code=400)

                    from notmyfault.plugin_schema import validate_plugin_meta
                    plugin_type = "trigger" if ptype == "triggers" else "action"
                    ok, errors = validate_plugin_meta(meta, plugin_type)
                    if not ok:
                        return JSONResponse({"ok": False, "error": "schema 校验失败: " + "; ".join(errors[:3])}, status_code=400)

                    pid = meta.get("id", root_name)
                    user_dir = self._get_user_plugins_dir()
                    dest = os.path.join(user_dir, ptype, pid)
                    if os.path.exists(dest):
                        return JSONResponse({"ok": False, "error": f"插件 '{pid}' 已存在"}, status_code=400)

                    os.makedirs(dest, exist_ok=True)
                    for member in zf.namelist():
                        rel = os.path.relpath(member, root_name)
                        if rel == "." or rel.startswith(".."):
                            continue
                        target = os.path.join(dest, rel)
                        if member.endswith("/"):
                            os.makedirs(target, exist_ok=True)
                        else:
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            with zf.open(member) as src, open(target, "wb") as dst:
                                dst.write(src.read())

                    return {"ok": True, "id": pid, "type": ptype, "restart_required": True}
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass

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

                loop = asyncio.get_running_loop()
                client_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
                subscriber = (loop, client_queue)
                with self._sub_lock:
                    self._subscribers.append(subscriber)

                try:
                    while True:
                        if await request.is_disconnected():
                            break

                        try:
                            event = await asyncio.wait_for(client_queue.get(), timeout=15)
                        except asyncio.TimeoutError:
                            yield ": keep-alive\n\n"
                            continue

                        yield f"event: {event['type']}\n"
                        yield f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                finally:
                    with self._sub_lock:
                        if subscriber in self._subscribers:
                            self._subscribers.remove(subscriber)

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

    def serve(self, host: str = "127.0.0.1", port: int = 19198):
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
            self._server.run()
        except OSError as e:
            code = getattr(e, 'winerror', None)
            if str(code) == "10048" or "10048" in str(e) or "bind" in str(e).lower():
                print(f"\n[提示] 端口 {port} 已被占用 — 引擎可能已在运行")
            else:
                raise
        except SystemExit:
            # uvicorn 内部通过 sys.exit() 完成干净关闭，这是预期行为
            print(f"\n[API] HTTP 服务已退出")
