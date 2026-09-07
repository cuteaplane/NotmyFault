import os
import glob
import json
import os
import sys
import threading
from typing import Any, Callable, Dict, Optional

from notmyfault.config import SignedConfigStore
from notmyfault.core.engine import AutomationEngine
from notmyfault.security.security import SecurityMode, detect_security_mode
from notmyfault.host.api.plugin_installation import (
    PluginBackupStore,
    PluginFileSystem,
    RetainedPluginBackup,
)
def _notify_build_required(detail: str) -> None:
    try:
        from notmyfault.host.alert import alert_user

        alert_user(
            "NotmyFault 安装文件不完整",
            f"{detail}。为防止安全模式被自动降低，引擎已拒绝启动。"
            "请从可信来源重新安装 NotmyFault。源码开发者可在确认文件完整后重新构建。",
            open_dashboard=False,
        )
    except Exception:
        print("[Startup] 无法发送安装完整性提示", file=sys.stderr)


def _shipped_plugin_directories(package_root: str) -> list[str]:
    plugin_specs = (
        ("actions", "action.json"),
        ("triggers", "trigger.json"),
    )
    return [
        os.path.dirname(meta_path)
        for relative_root, json_name in plugin_specs
        for meta_path in glob.glob(
            os.path.join(package_root, relative_root, "*", json_name)
        )
    ]


def _ensure_first_run_build(package_root: str) -> None:
    if getattr(sys, "frozen", False):
        return
    project_root = os.path.dirname(package_root)
    required_files = [
        os.path.join(project_root, "build.json"),
        os.path.join(project_root, "build.json.sig"),
    ]
    missing = [path for path in required_files if not os.path.isfile(path)]
    if detect_security_mode() == SecurityMode.STRICT:
        missing.extend(
            os.path.join(plugin_dir, "signature.sig")
            for plugin_dir in _shipped_plugin_directories(package_root)
            if not os.path.isfile(os.path.join(plugin_dir, "signature.sig"))
        )
    if not missing:
        return
    relative = [os.path.relpath(path, project_root) for path in missing]
    detail = "缺少签名文件：" + "、".join(relative[:5])
    if len(relative) > 5:
        detail += f" 等 {len(relative)} 个文件"
    _notify_build_required(detail)
    raise RuntimeError(detail)


def _get_plugin_paths(store: SignedConfigStore):
    paths = []
    paths.append((str(store.paths.package_root), "builtin"))
    user_dir = str(store.paths.user_plugins_dir)
    if os.path.isdir(user_dir):
        paths.append((user_dir, "user"))
    return paths


def migrate_user_plugin_enabled_state(
    config: Dict[str, Any],
    store: SignedConfigStore,
    user_dir: Optional[str] = None,
) -> list[str]:
    if user_dir is None:
        user_dir = str(store.paths.user_plugins_dir)
    if not os.path.isdir(user_dir):
        return []

    disabled = config.get("disabled_plugins", {})
    if not isinstance(disabled, dict):
        disabled = {"triggers": [], "actions": []}
        config["disabled_plugins"] = disabled

    migrated: list[str] = []
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
                migrated.append(pid)

    if not migrated:
        return []

    if not store.save_config(config):
        print("[Plugins] 迁移开关状态后保存 config 失败", file=sys.stderr)
        return []

    print(
        f"[Plugins] 已把 {len(migrated)} 个用户插件的开关状态迁到 config: "
        f"{', '.join(migrated)}"
    )
    return migrated


def _plugin_check_needed(
    backup: RetainedPluginBackup,
    config: Dict[str, Any],
) -> bool:
    meta = backup.active_meta
    if not isinstance(meta, dict):
        return True
    plugin_id = meta.get("id")
    if not isinstance(plugin_id, str) or not plugin_id:
        return True
    disabled = config.get("disabled_plugins", {})
    disabled_key = "triggers" if backup.kind == "trigger" else "actions"
    if isinstance(disabled, dict):
        disabled_plugins = disabled.get(disabled_key, [])
        if isinstance(disabled_plugins, list) and plugin_id in disabled_plugins:
            return False

    from notmyfault.platform.capabilities import is_capability_compatible
    from notmyfault.security.plugin_loader import is_plugin_platform_compatible

    if not is_plugin_platform_compatible(meta):
        return False
    capability_ok, _problems = is_capability_compatible(meta)
    return capability_ok


def _notify_plugin_recovery(
    restored: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    on_event: Optional[Callable[[str, Dict[str, Any]], None]],
) -> None:
    if restored:
        payload = {"plugins": restored}
        if on_event is not None:
            try:
                on_event("plugin_update_rolled_back", payload)
            except Exception:
                pass
        names = ", ".join(
            str(item.get("id") or item.get("package_name"))
            for item in restored
        )
        try:
            from notmyfault.host.alert import alert_user

            alert_user(
                "插件更新已撤回",
                f"更新版加载失败，已恢复旧版：{names}",
                open_dashboard=True,
            )
        except Exception:
            print("[Plugins] 无法发送插件恢复提示", file=sys.stderr)
    if failed and on_event is not None:
        try:
            on_event("plugin_update_rollback_failed", {"plugins": failed})
        except Exception:
            pass


def _recover_failed_plugin_updates(
    engine: AutomationEngine,
    config: Dict[str, Any],
    store: SignedConfigStore,
    load_paths: list[tuple[str, str]],
    on_event: Optional[Callable[[str, Dict[str, Any]], None]],
    file_system: PluginFileSystem,
) -> AutomationEngine:
    backups = PluginBackupStore(
        store.paths.user_plugins_dir,
        file_system,
    )
    retained = backups.retained()
    broken: list[RetainedPluginBackup] = []
    unresolved: list[dict[str, Any]] = []
    for backup in retained:
        if backup.problem:
            unresolved.append(
                {
                    "backup": str(backup.backup_path),
                    "error": backup.problem,
                }
            )
            continue
        if not _plugin_check_needed(backup, config):
            continue
        meta = backup.active_meta
        plugin_id = meta.get("id") if isinstance(meta, dict) else None
        if not isinstance(plugin_id, str) or not plugin_id:
            broken.append(backup)
            continue
        if not engine.check_plugin_load(
            backup.kind,
            plugin_id,
            str(backup.active_path),
        ):
            broken.append(backup)
    if not broken:
        _notify_plugin_recovery([], unresolved, on_event)
        return engine

    engine.shutdown()
    restored: list[dict[str, Any]] = []
    for backup in broken:
        try:
            backups.restore(backup)
        except OSError as error:
            unresolved.append(
                {
                    "backup": str(backup.backup_path),
                    "error": str(error),
                }
            )
            print(
                f"[Plugins] 恢复插件备份失败 ({backup.backup_path}): {error}",
                file=sys.stderr,
            )
            continue
        restored.append(
            {
                "id": backup.backup_meta.get("id"),
                "package_name": backup.backup_meta.get("package_name"),
                "version_code": backup.backup_meta.get("version_code"),
            }
        )
        print(
            f"[Plugins] 更新版加载失败，已恢复 {backup.backup_meta.get('id')}",
            file=sys.stderr,
        )

    replacement = AutomationEngine(
        config,
        on_event=on_event,
        rules_store=store,
    )
    replacement.auto_load(load_paths)
    for backup in broken:
        if not any(item.get("id") == backup.backup_meta.get("id") for item in restored):
            continue
        plugin_id = backup.backup_meta.get("id")
        if isinstance(plugin_id, str) and not replacement.check_plugin_load(
            backup.kind,
            plugin_id,
            str(backup.destination),
        ):
            unresolved.append(
                {
                    "backup": str(backup.destination),
                    "error": "旧版插件仍然无法加载",
                }
            )
    _notify_plugin_recovery(restored, unresolved, on_event)
    return replacement


def create_engine(
    store: SignedConfigStore,
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    plugin_file_system: PluginFileSystem | None = None,
) -> AutomationEngine:
    _ensure_first_run_build(str(store.paths.package_root))
    config = store.load_config()
    migrate_user_plugin_enabled_state(config, store)
    config["rules"] = store.load_rules()
    load_paths = _get_plugin_paths(store)
    engine = AutomationEngine(config, on_event=on_event, rules_store=store)
    engine.auto_load(load_paths)
    return _recover_failed_plugin_updates(
        engine,
        config,
        store,
        load_paths,
        on_event,
        plugin_file_system or PluginFileSystem(),
    )


def run(
    store: SignedConfigStore,
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
    engine = create_engine(store=store, on_event=on_event)
    engine.start(shutdown_event=shutdown_event)
