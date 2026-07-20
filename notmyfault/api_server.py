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
from notmyfault.plugin_schema import scan_plugins, validate_plugin_meta
from notmyfault.security import detect_security_mode

_scan_plugins = scan_plugins  # 向后兼容

# API 认证令牌（每次进程启动时随机生成）
API_TOKEN: str = secrets.token_hex(32)
# token 文件存放在 %APPDATA%/NotmyFault/ 下（与 config.json 同目录），
# 不再使用 %TEMP%——TEMP 目录默认 Authenticated Users 可读，权限过宽。
API_TOKEN_FILE: str = os.path.join(os.path.dirname(CONFIG_FILE), ".api_token")


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
        self._subscribers: list["asyncio.Queue"] = []

        self._loop = None
        self._sub_lock = threading.Lock()
        self._server = None
        self._engine_ref = None

        self.app = FastAPI(title="NotmyFault Engine API", version="alpha-0.10")
        self._setup_middleware()
        self._setup_routes()
        global API_TOKEN
        API_TOKEN = secrets.token_hex(32)
        _secure_write_token(API_TOKEN_FILE, API_TOKEN)

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
        if request.method == "GET":
            return
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
        if not token or not secrets.compare_digest(token, API_TOKEN):
            print(f"[Auth] 403 rejected: has_hdr={bool(auth)} req_len={len(token)} srv_len={len(API_TOKEN)}",
                  file=sys.stderr)
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
                return {"ok": True, "running": self._engine.engine_running,

                "api_alive": True, "message": "already_running"}

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
                "api_alive": True,
                "engine_running": self._engine.engine_running,
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
                "security_mode": detect_security_mode().value,
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

            # 安全校验：命中危险模式（危险命令/危险路径）则拒绝写入
            from notmyfault.config import _validate_rules_safety
            _warnings, errors = _validate_rules_safety(body.get("rules", []))
            if errors:
                return JSONResponse(
                    {"ok": False, "error": "规则安全校验失败", "details": errors[:10]},
                    status_code=400,
                )

            existing_config = self._load_config()
            new_config = {
                "rules": body.get("rules", []),
                "disabled_plugins": existing_config.get("disabled_plugins", {"triggers": [], "actions": []}),
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
            import tempfile, shutil, py7zr
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

                # 找插件根目录：优先单文件夹，兼容文件平铺的打包方式
                entries = os.listdir(extract_dir)
                dirs = [d for d in entries if os.path.isdir(os.path.join(extract_dir, d))]
                if len(dirs) == 1:
                    root_path = os.path.join(extract_dir, dirs[0])
                elif len(dirs) == 0:
                    # 文件直接平铺在 archive 根目录
                    root_path = extract_dir
                else:
                    return JSONResponse({"ok": False, "error": "nmfp 根目录应恰好有一个插件文件夹"}, status_code=400)

                # 确定类型
                has_trigger = os.path.exists(os.path.join(root_path, "trigger.json"))
                has_action = os.path.exists(os.path.join(root_path, "action.json"))
                if has_trigger:
                    ptype, json_name = "triggers", "trigger.json"
                elif has_action:
                    ptype, json_name = "actions", "action.json"
                else:
                    return JSONResponse({"ok": False, "error": "未找到 trigger.json 或 action.json"}, status_code=400)

                # 校验 metadata
                try:
                    with open(os.path.join(root_path, json_name), "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except (json.JSONDecodeError, OSError):
                    return JSONResponse({"ok": False, "error": "插件元数据 JSON 损坏或缺失"}, status_code=400)

                from notmyfault.plugin_schema import validate_plugin_meta
                plugin_type = "trigger" if ptype == "triggers" else "action"
                ok, errors = validate_plugin_meta(meta, plugin_type)
                if not ok:
                    return JSONResponse({"ok": False, "error": "schema 校验失败: " + "; ".join(errors[:3])}, status_code=400)

                pkg = meta.get("package_name", "")
                new_vc = meta.get("version_code", 0)
                pid = meta.get("id", os.path.basename(root_path))
                force = str(form.get("force", "")).lower() in ("1", "true", "yes")
                user_dir = self._get_user_plugins_dir()

                # 路径穿越防御：pid 不可含路径分隔符或 ..
                if not self._is_safe_plugin_id(pid):
                    return JSONResponse(
                        {"ok": False, "error": "插件 id 含非法字符（禁止路径分隔符）"},
                        status_code=400,
                    )
                dest = os.path.join(user_dir, ptype, pid)
                # 二次校验：normpath 后 dest 必须仍在 user_dir/ptype 下
                ptype_dir = os.path.join(user_dir, ptype)
                if not os.path.normpath(dest).startswith(
                    os.path.normpath(ptype_dir) + os.sep
                ):
                    return JSONResponse(
                        {"ok": False, "error": "插件路径越界"},
                        status_code=400,
                    )

                # 全局包名唯一性检查 + 版本对比
                existing = self._find_plugin_by_package(pkg)
                if existing:
                    ex_ptype, ex_pid, ex_meta = existing
                    ex_vc = ex_meta.get("version_code", 0)
                    # 相同版本号也要求 force（之前只拦截降级 new_vc < ex_vc，
                    # 导致 new_vc == ex_vc 时静默覆盖用户已修改的插件配置）。
                    if not force and new_vc <= ex_vc:
                        return JSONResponse(
                            {"ok": False,
                             "error": f"版本不高于当前 v{ex_vc}（packageName={pkg}）。强制覆盖请传 force=true"},
                            status_code=400)
                    # 删除旧插件（允许跨类型升级/覆盖）
                    import shutil as _sh
                    _sh.rmtree(os.path.join(user_dir, ex_ptype, ex_pid), ignore_errors=True)

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

                # 复制到 user plugins（清空旧目录后写入）
                # 之前只复制文件（os.path.isfile），跳过子目录，
                # 导致含 assets/、lib/ 等子目录的插件资源丢失。
                # 改用 shutil.copytree 递归复制，ignore 跳过 __pycache__ 缓存。
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
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
                if extract_dir:
                    shutil.rmtree(extract_dir, ignore_errors=True)

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
