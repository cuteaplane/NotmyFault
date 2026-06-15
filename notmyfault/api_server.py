"""
NotmyFault HTTP API 服务器
——————————————————————————
本地 REST API + SSE 事件流，替代w***d的 IPC 层。

引擎启动后，UI 通过 HTTP 请求控制引擎，通过 SSE 接收实时事件。
可以用任何浏览器打开 dashboard.html 直接管理。
"""

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Callable, Protocol

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

from notmyfault.config import CONFIG_FILE


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
# 插件 schema 扫描 (复刻 engine.py 的 auto_load 扫描逻辑)
# ---------------------------------------------------------------------------

def _scan_plugins(base_dir: str, plugins_dir: str, json_filename: str) -> Dict[str, Dict]:
    """扫描插件目录，返回 {plugin_id: metadata} 的字典"""
    result: Dict[str, Dict] = {}
    root = os.path.join(base_dir, plugins_dir)
    if not os.path.isdir(root):
        return result

    for folder_name in os.listdir(root):
        folder_path = os.path.join(root, folder_name)
        if not os.path.isdir(folder_path):
            continue

        json_file = os.path.join(folder_path, json_filename)
        if not os.path.exists(json_file):
            continue

        try:
            with open(json_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue

        plugin_id = meta.get("id")
        if plugin_id:
            result[plugin_id] = meta

    return result


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
        self._event_queue: queue.Queue = queue.Queue(maxsize=100)
        self._event_signal = threading.Event()
        self._server = None

        self.app = FastAPI(title="NotmyFault Engine API", version="1.0")
        self._setup_middleware()
        self._setup_routes()

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
        """
        引擎线程调用此方法推送事件。
        线程安全 — 使用 queue + event 唤醒 SSE 生成器。
        """
        try:
            self._event_queue.put_nowait({
                "type": event_type,
                "data": data,
                "ts": time.time(),
            })
            self._event_signal.set()  # 立即唤醒 SSE 轮询
        except queue.Full:
            pass  # 队列满了就丢弃，不阻塞引擎

    # ---- 插件扫描 (供 /api/plugins 使用) --------------------------------

    def _get_plugins_schema(self) -> Dict[str, Any]:
        base = os.path.dirname(__file__)
        return {
            "triggers": _scan_plugins(base, "triggers", "trigger.json"),
            "actions": _scan_plugins(base, "actions", "action.json"),
        }

    # ---- 配置读写辅助 ----------------------------------------------------

    def _load_config(self) -> Dict[str, Any]:
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {"rules": []}

    def _save_config(self, config: Dict[str, Any]) -> bool:
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            print(f"[API] 保存配置失败: {e}")
            return False

    # ---- 路由注册 --------------------------------------------------------

    def _setup_routes(self):
        app = self.app

        # ================================================================
        # 引擎控制
        # ================================================================

        @app.post("/api/engine/start")
        async def engine_start():
            print("[API] POST /api/engine/start")
            if self._engine.engine_running:
                return {"ok": True, "running": True, "message": "already_running"}

            self._engine._start_engine_core()
            return {"ok": True, "running": self._engine.engine_running}

        @app.post("/api/engine/stop")
        def engine_stop():
            print("[API] POST /api/engine/stop")
            self._engine._stop_engine()
            return {"ok": True}

        @app.post("/api/engine/shutdown")
        def engine_shutdown():
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
        # 插件 schema
        # ================================================================

        @app.get("/api/plugins")
        async def plugins_schema():
            return self._get_plugins_schema()

        # ================================================================
        # SSE 事件流
        # ================================================================

        @app.get("/api/events")
        async def event_stream(request: Request):
            """
            Server-Sent Events 流。
            引擎事件通过 queue + event 机制从引擎线程传递到这里。
            客户端断开时自动清理。
            """

            async def generate():
                # 为每个客户端创建独立的本地引用
                q = self._event_queue
                ev = self._event_signal

                while True:
                    # 检查客户端是否断开
                    if await request.is_disconnected():
                        break

                    # 非阻塞排空队列
                    drained = False
                    while True:
                        try:
                            event = q.get_nowait()
                            drained = True
                            yield f"event: {event['type']}\n"
                            yield f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                        except queue.Empty:
                            break

                    if drained:
                        continue  # 立即再检查一次，不 sleep

                    # 等待新事件或超时（用短 sleep 替代 threading.Event 的跨线程等待）
                    # 在 asyncio 中不能直接 block，所以用 run_in_executor
                    import asyncio
                    loop = asyncio.get_event_loop()

                    def wait_signal():
                        ev.wait(1.0)
                        ev.clear()

                    await loop.run_in_executor(None, wait_signal)

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
        except (OSError, SystemExit) as e:
            code = getattr(e, 'winerror', None) or getattr(e, 'code', None)
            if str(code) == "10048" or "10048" in str(e) or "bind" in str(e).lower():
                print(f"\n[提示] 端口 {port} 已被占用 — 引擎可能已在运行")
            elif isinstance(e, SystemExit):
                print(f"\n[API] HTTP 服务已退出")
            else:
                raise
            # 不 raise — 让主线程自然进入 cleanup
