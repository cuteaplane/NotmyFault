import os
import sys
import threading
from typing import Any, Callable, Dict, Optional

from notmyfault.config import get_config, get_rules, save_config
from notmyfault.core.engine import AutomationEngine
from notmyfault.platform.platform_support import get_config_dir
import glob
import json
import subprocess as _sp


def _run_build_command(cmd: list[str], build_dir: str):
    """以 UTF-8 运行 build.py，首次启动日志使用统一编码。"""
    env = os.environ.copy()
    # build.py 输出中文，子进程和父进程都显式使用 UTF-8。
    env["PYTHONUTF8"] = "1"
    return _sp.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=build_dir,
        env=env,
        timeout=60,
    )


def _notify_first_run_mode(mode: str) -> None:
    """首次构建或降级后通知用户当前安全模式。"""
    try:
        from notmyfault.host.alert import alert_user
        alert_user(
            "NotmyFault 首次运行",
            f"检测到缺少签名，已自动完成开发构建并进入 {mode} 安全模式。"
            "如需更严格的安全模式，请运行 `python build.py build`（strict）"
            "并在 Dashboard「安全与权限」页确认当前配置。",
            open_dashboard=False,
        )
    except Exception:
        print("[FirstRun] 无法发送安全模式提示", file=sys.stderr)


_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _shipped_plugin_directories() -> list[str]:
    plugin_specs = (
        ("actions", "action.json"),
        ("triggers", "trigger.json"),
        (os.path.join("bundled", "actions"), "action.json"),
        (os.path.join("bundled", "triggers"), "trigger.json"),
    )
    return [
        os.path.dirname(meta_path)
        for relative_root, json_name in plugin_specs
        for meta_path in glob.glob(
            os.path.join(_PKG_ROOT, relative_root, "*", json_name)
        )
    ]


def _ensure_first_run_build() -> None:
    if getattr(sys, "frozen", False):
        return
    src_dir = _PKG_ROOT
    build_json = os.path.join(src_dir, "..", "build.json")
    build_py = os.path.join(src_dir, "..", "build.py")
    plugin_dirs = _shipped_plugin_directories()
    needs_build = not os.path.exists(build_json) or any(
        not os.path.isfile(os.path.join(plugin_dir, "signature.sig"))
        for plugin_dir in plugin_dirs
    )
    if not needs_build:
        return
    print("[FirstRun] Detected missing signatures or build file, running first-time build...")
    # GUI 首次启动没有终端输入私钥口令，因此使用 permissive 构建生成本机签名文件。
    # 正式发布由开发者显式执行 strict 构建。
    commands = [
        ([sys.executable, build_py, "build", "--security-mode=permissive"], "build"),
    ]
    for cmd, name in commands:
        try:
            result = _run_build_command(cmd, os.path.dirname(build_py))
            if result.returncode != 0:
                print(f"[FirstRun] build {name} failed (code={result.returncode}): {result.stderr.strip()[:500]}")
                _degrade_security_mode()
                return
            print(f"[FirstRun] build {name} OK")
        except Exception as e:
            print(f"[FirstRun] build {name} exception: {e}")
            _degrade_security_mode()
            return
    print("[FirstRun] First-time build complete")
    _notify_first_run_mode("permissive")


def _degrade_security_mode() -> None:
    env_mode = os.environ.get("NOTMYFAULT_MODE", "")
    if env_mode:
        return
    os.environ["NOTMYFAULT_MODE"] = "develop"
    print("[FirstRun] Degraded to development mode (NOTMYFAULT_MODE=develop)")
    _notify_first_run_mode("develop（normal）")


def _get_plugin_paths():
    paths = []
    if getattr(sys, "frozen", False):
        paths.append((os.path.join(sys._MEIPASS, "notmyfault"), "builtin"))
    else:
        paths.append((_PKG_ROOT, "builtin"))
    user_dir = os.path.join(get_config_dir(), "plugins")
    if os.path.isdir(user_dir):
        paths.append((user_dir, "user"))
    return paths


def migrate_user_plugin_enabled_state(
    config: Dict[str, Any], user_dir: Optional[str] = None
) -> list[str]:
    """旧版开关插件会直接改用户插件的签名 json，这里把 json 里的 enabled: false 搬进 config 的 disabled_plugins，并把 json 改回 true，返回迁移的插件 id 列表。"""
    if user_dir is None:
        user_dir = os.path.join(get_config_dir(), "plugins")
    if not os.path.isdir(user_dir):
        return []

    disabled = config.get("disabled_plugins", {})
    if not isinstance(disabled, dict):
        disabled = {"triggers": [], "actions": []}
        config["disabled_plugins"] = disabled

    migrated: list[str] = []
    pending_rewrites: list[tuple[str, Dict[str, Any]]] = []
    for ptype, json_name in (("triggers", "trigger.json"), ("actions", "action.json")):
        ptype_root = os.path.join(user_dir, ptype)
        if not os.path.isdir(ptype_root):
            continue
        disabled_list = disabled.get(ptype, [])
        if not isinstance(disabled_list, list):
            disabled_list = []
            disabled[ptype] = disabled_list
        for folder_name in sorted(os.listdir(ptype_root)):
            json_path = os.path.join(ptype_root, folder_name, json_name)
            if not os.path.isfile(json_path):
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(meta, dict) or meta.get("enabled") is not False:
                continue
            pid = meta.get("id") or folder_name
            if pid not in disabled_list:
                disabled_list.append(pid)
            meta["enabled"] = True
            pending_rewrites.append((json_path, meta))
            migrated.append(pid)

    if not migrated:
        return []

    # config 先落盘再改 json，保存失败时插件保持 json 里的禁用状态。
    if not save_config(config):
        print("[Plugins] 迁移开关状态后保存 config 失败，json 保持不动", file=sys.stderr)
        return []

    from notmyfault.security.plugins import (
        compute_file_hash,
        load_plugin_manifest,
        save_plugin_manifest,
    )

    manifest = load_plugin_manifest()
    manifest_changed = False
    for json_path, meta in pending_rewrites:
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"[Plugins] 迁移 {meta.get('id')} 失败，写回 json 出错: {e}", file=sys.stderr)
            continue
        # 改过的 json 重新记一次哈希基线，不然完整性校验会一直报文件被修改。
        pid = meta.get("id")
        json_name = os.path.basename(json_path)
        new_hash = compute_file_hash(json_path)
        if pid and new_hash is not None:
            manifest.setdefault(pid, {})[json_name] = new_hash
            manifest_changed = True
    if manifest_changed:
        save_plugin_manifest(manifest)

    print(
        f"[Plugins] 已把 {len(migrated)} 个用户插件的开关状态迁到 config: "
        f"{', '.join(migrated)}；这些插件的签名已失效，重新安装插件包可恢复"
    )
    return migrated


def create_engine(
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> AutomationEngine:
    _ensure_first_run_build()
    config = get_config()
    migrate_user_plugin_enabled_state(config)
    # 规则拆到 rules.json 后单独加载，引擎仍收带 rules 的合并 dict
    config["rules"] = get_rules()
    engine = AutomationEngine(config, on_event=on_event)
    engine.auto_load(_get_plugin_paths())
    return engine


def run(
    shutdown_event: "threading.Event | None" = None,
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> None:
    # 引擎以普通权限启动，插件提权通过 notmyfault.security.sudo 请求 UAC 授权。
    from notmyfault.security.security import is_admin_process
    if is_admin_process():
        print(
            "[Engine] [!!] NotmyFault 拒绝以管理员身份启动：请用普通用户权限运行。"
            "插件需要提权时请通过 notmyfault.security.sudo.run_as_admin 弹出 UAC 授权。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    engine = create_engine(on_event=on_event)
    engine.start(shutdown_event=shutdown_event)
