from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict

from notmyfault.application_paths import ApplicationPaths
from notmyfault.config import ConfigValidationError, SignedConfigStore
from notmyfault.core.logging import engine_error
from notmyfault.host.api.plugin_installation import (
    PendingPreviewStore,
    PluginFileSystem,
    PluginInstallTransaction,
    PluginTemporaryStorage,
    snapshot_plugin_directory,
)
from notmyfault.host.api.services.plugin_catalog import PluginCatalogService
from notmyfault.host.plugin_registry import (
    PluginRegistryError,
)
from notmyfault.host.api.ports import PluginRegistryPort
from notmyfault.security.plugin_schema import (
    check_permissions_conform,
    current_platform_name,
    get_permission_info,
    is_valid_plugin_id,
    scan_plugin_security,
    validate_plugin_meta,
)
from notmyfault.security.plugin_package import PluginPackageLimits, extract_nmfp
from notmyfault.security.plugins import plugin_signature_kind, scan_borrowed_privilege
from notmyfault.security.security import detect_security_mode


@dataclass(frozen=True, slots=True)
class PluginInstallationError(Exception):
    status_code: int
    body: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class PluginDownload:
    content: bytes
    filename: str
    sha256: str


class PluginInstallationService:
    def __init__(
        self,
        paths: ApplicationPaths,
        store: SignedConfigStore,
        catalog: PluginCatalogService,
        file_system: PluginFileSystem,
        previews: PendingPreviewStore,
        registry: PluginRegistryPort,
        temporary_storage: PluginTemporaryStorage | None = None,
        package_limits: PluginPackageLimits | None = None,
        build_hook: Callable[[Path, Dict[str, Any]], None] | None = None,
    ) -> None:
        self._paths = paths
        self._store = store
        self._catalog = catalog
        self._file_system = file_system
        self._transaction = PluginInstallTransaction(file_system)
        self._previews = previews
        self._registry = registry
        self._temporary_storage = temporary_storage or PluginTemporaryStorage()
        self._package_limits = package_limits or PluginPackageLimits()
        self._build_hook = build_hook or self._run_build_hook

    @property
    def max_upload_bytes(self) -> int:
        return self._package_limits.max_upload_bytes

    def registry(self, url: Any) -> Dict[str, Any]:
        try:
            registry = self._registry.load(url if isinstance(url, str) else "")
        except PluginRegistryError as error:
            self._fail(400, str(error))
        except Exception as error:
            raise PluginInstallationError(
                400,
                {"ok": False, "error": "读取插件索引失败"},
            ) from error
        return {"ok": True, **registry}

    def download_registry(self, body: Any) -> PluginDownload:
        try:
            if not isinstance(body, dict):
                raise PluginRegistryError("下载参数格式无效")
            package_name = body.get("package_name", "")
            version = body.get("version", "")
            if not isinstance(package_name, str) or not isinstance(version, str):
                raise PluginRegistryError("下载参数格式无效")
            archive, entry = self._registry.download(
                body.get("url", ""),
                package_name,
                version,
            )
        except PluginRegistryError as error:
            self._fail(400, str(error))
        except Exception as error:
            raise PluginInstallationError(
                400,
                {"ok": False, "error": "下载插件包失败"},
            ) from error
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", package_name)
        safe_version = re.sub(r"[^a-zA-Z0-9_.-]", "_", version)
        return PluginDownload(
            content=archive,
            filename=f"{safe_name}-{safe_version}.nmfp",
            sha256=entry["sha256"],
        )

    def toggle(self, plugin_kind: Any, plugin_id: Any) -> Dict[str, Any]:
        if plugin_kind not in ("triggers", "actions") or not plugin_id:
            self._fail(400, "需要 type (triggers/actions) 和 id")
        if not self._safe_plugin_id(plugin_id):
            return {"ok": False, "error": "插件 id 含非法字符（禁止路径分隔符）"}
        json_name = (
            "trigger.json" if plugin_kind == "triggers" else "action.json"
        )
        user_json = (
            self._paths.user_plugins_dir / plugin_kind / plugin_id / json_name
        )
        builtin_json = (
            self._paths.package_root / plugin_kind / plugin_id / json_name
        )
        if user_json.exists():
            origin = "user"
        elif builtin_json.exists():
            origin = "builtin"
        else:
            return {"ok": False, "error": "插件不存在"}
        try:
            config = self._load_config_for_update()
            disabled = config.get("disabled_plugins", {})
            if not isinstance(disabled, dict):
                disabled = {"triggers": [], "actions": []}
            disabled_list = disabled.get(plugin_kind, [])
            if not isinstance(disabled_list, list):
                disabled_list = []
            if plugin_id in disabled_list:
                disabled_list.remove(plugin_id)
                enabled = True
            else:
                disabled_list.append(plugin_id)
                enabled = False
            disabled[plugin_kind] = disabled_list
            config["disabled_plugins"] = disabled
            if not self._store.save_config(config):
                return {"ok": False, "error": "无法保存配置"}
            return {
                "ok": True,
                "enabled": enabled,
                "origin": origin,
                "restart_required": True,
            }
        except ConfigValidationError:
            return {"ok": False, "error": "配置未通过完整性校验"}
        except Exception:
            return {"ok": False, "error": "切换插件状态失败"}

    def uninstall(self, plugin_kind: str, plugin_id: str) -> Dict[str, Any]:
        if plugin_kind not in ("triggers", "actions"):
            self._fail(400, "type 必须为 triggers 或 actions")
        if not self._safe_plugin_id(plugin_id):
            return {"ok": False, "error": "插件 id 含非法字符（禁止路径分隔符）"}
        json_name = (
            "trigger.json" if plugin_kind == "triggers" else "action.json"
        )
        plugin_dir = self._paths.user_plugins_dir / plugin_kind / plugin_id
        if not (plugin_dir / json_name).exists():
            return {"ok": False, "error": "只能卸载用户插件，或插件不存在"}
        try:
            meta = json.loads((plugin_dir / json_name).read_text(encoding="utf-8"))
            package_name = meta.get("package_name") if isinstance(meta, dict) else None
        except (OSError, ValueError):
            package_name = None
        backups = {plugin_dir.with_name(plugin_dir.name + ".nmf-backup")}
        if isinstance(package_name, str) and package_name:
            backups.update(self._backups_for_package(package_name))
        try:
            self._file_system.remove_tree(plugin_dir)
            for backup in backups:
                self._file_system.remove_tree(backup)
        except OSError:
            return {"ok": False, "error": "删除插件文件失败"}
        return {"ok": True, "restart_required": True}

    def key_status(self) -> Dict[str, Any]:
        private_key = self._paths.plugin_private_key_file
        if not private_key.exists():
            return {"exists": False, "encrypted": False}
        with open(private_key, "rb") as file:
            header = file.read(100)
        return {
            "exists": True,
            "encrypted": b"ENCRYPTED" in header,
        }

    def scan_install_risks(
        self,
        root_path: str,
        json_name: str,
        meta: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        risks = scan_plugin_security(root_path)
        build = meta.get("build")
        if isinstance(build, dict) and (
            build.get("command") or build.get("outputs")
        ):
            risks.append(
                {
                    "id": "build_hook",
                    "label": "安装时执行构建命令",
                    "level": "high",
                    "detail": "点击安装后会以当前用户身份执行插件包声明的 build 命令。",
                    "file": json_name,
                }
            )
        borrowed_findings = []
        for py_file in sorted(Path(root_path).rglob("*.py")):
            if py_file.is_file():
                borrowed_findings.extend(scan_borrowed_privilege(str(py_file)))
        if borrowed_findings:
            risks.append(
                {
                    "id": "borrowed_privilege",
                    "label": "借壳提权嫌疑",
                    "level": "high",
                    "detail": "插件代码可能借其他已授权插件的身份请求管理员权限: "
                    + "；".join(sorted(set(borrowed_findings))[:3]),
                    "file": "*.py",
                }
            )
        plugin_kind = "triggers" if json_name == "trigger.json" else "actions"
        collision = self._catalog.plugin_id_collision(plugin_kind, meta)
        if collision is not None:
            risks.append(
                {
                    "id": "plugin_id_collision",
                    "label": "插件 id 已被其他包使用",
                    "level": "high",
                    "detail": (
                        f"{meta.get('id', '')} 已属于包 "
                        f"{collision[2].get('package_name', '')}"
                    ),
                    "file": json_name,
                }
            )
        return risks

    def preview(self, data: bytes, password: str = "") -> Dict[str, Any]:
        self._previews.purge_expired()
        if len(data) > self._package_limits.max_upload_bytes:
            self._fail(
                400,
                "插件包过大（>"
                f"{self._package_limits.max_upload_bytes // (1024 * 1024)}MB）",
            )
        archive_path = self._temporary_storage.write_archive(data)
        extract_dir: Path | None = None
        try:
            extract_dir = self._temporary_storage.create_directory()
            extract_nmfp(
                str(archive_path),
                str(extract_dir),
                password,
                self._package_limits,
            )
            root_path = self._plugin_root(extract_dir)
            plugin_kind, json_name = self._plugin_kind(root_path)
            meta = self._read_manifest(root_path / json_name)
            plugin_type = "trigger" if plugin_kind == "triggers" else "action"
            schema_valid, schema_errors = validate_plugin_meta(meta, plugin_type)
            risks = self.scan_install_risks(str(root_path), json_name, meta)
            permissions = []
            for permission in meta.get("permissions", []):
                info = get_permission_info(permission)
                if info:
                    permissions.append(
                        {
                            "permission": permission,
                            "label": info["label"],
                            "risk": info["risk"],
                            "description": info["description"],
                            "known": True,
                        }
                    )
                else:
                    permissions.append(
                        {
                            "permission": permission,
                            "label": permission,
                            "risk": "unknown",
                            "description": "未知权限，不在安全规范中",
                            "known": False,
                        }
                    )
            permission_conform, permission_errors = check_permissions_conform(
                meta.get("permissions", [])
            )
            update_diff = self._update_diff(str(root_path), meta)
            file_snapshot = snapshot_plugin_directory(str(root_path))
            if file_snapshot is None:
                self._fail(400, "插件目录包含链接或无法完整读取")
            preview_token = self._previews.create(
                {
                    "extract_dir": str(extract_dir),
                    "root_path": str(root_path),
                    "meta": meta,
                    "ptype": plugin_kind,
                    "json_name": json_name,
                    "file_snapshot": file_snapshot,
                    "risk_ids": [risk.get("id") for risk in risks],
                }
            )
            extract_dir = None
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
                    "type": plugin_kind,
                    "semantic": meta.get("semantic", ""),
                    "platforms": meta.get("platforms", []),
                    "entrypoints": meta.get("entrypoints", {}),
                    "platform_compatible": self._platform_compatible(meta),
                },
                "permissions": permissions,
                "permission_conform": permission_conform,
                "permission_errors": permission_errors,
                "risks": risks,
                "schema_valid": schema_valid,
                "schema_errors": schema_errors[:5] if schema_errors else [],
                "update_diff": update_diff,
            }
        except PluginInstallationError:
            raise
        except ValueError as error:
            raise PluginInstallationError(
                400,
                {"ok": False, "error": "插件包未通过安全检查"},
            ) from error
        finally:
            self._temporary_storage.remove_file(archive_path)
            if extract_dir is not None:
                self._file_system.discard_tree(extract_dir)

    def install(
        self,
        data: bytes | None,
        preview_token: str = "",
        password: str = "",
        signing_password: str = "",
        force: bool = False,
        confirmed_risk_ids: Any = None,
    ) -> Dict[str, Any]:
        self._previews.purge_expired()
        extract_dir: Path | None = None
        archive_path: Path | None = None
        accepted_risk_ids: set[Any] = set()
        preview_owned = False
        try:
            if preview_token and preview_token in self._previews:
                preview = self._previews[preview_token]
                root_path = Path(preview["root_path"])
                plugin_kind = preview["ptype"]
                json_name = preview["json_name"]
                extract_dir = Path(preview["extract_dir"])
                accepted_risk_ids = set(preview.get("risk_ids") or [])
                current_snapshot = snapshot_plugin_directory(str(root_path))
                if (
                    current_snapshot is None
                    or current_snapshot != preview.get("file_snapshot")
                ):
                    self._previews.discard(preview_token)
                    extract_dir = None
                    self._fail(409, "插件预览内容已经变化，请重新预览")
                meta = self._read_manifest(root_path / json_name, preview=True)
                plugin_type = (
                    "trigger" if plugin_kind == "triggers" else "action"
                )
                valid, _errors = validate_plugin_meta(meta, plugin_type)
                if not valid:
                    self._previews.discard(preview_token)
                    extract_dir = None
                    self._fail(400, "插件元数据未通过校验，请重新预览")
                current_risks = self.scan_install_risks(
                    str(root_path), json_name, meta
                )
                if any(
                    risk.get("id") not in accepted_risk_ids
                    for risk in current_risks
                ):
                    self._previews.discard(preview_token)
                    extract_dir = None
                    self._fail(409, "插件安全检查结果已经变化，请重新预览")
                confirmed = self._parse_confirmed_risk_ids(confirmed_risk_ids)
                current_risk_ids = {
                    str(risk.get("id"))
                    for risk in current_risks
                    if isinstance(risk.get("id"), str)
                }
                if not confirmed.issubset(current_risk_ids):
                    self._fail(400, "确认的风险项与当前预览不一致")
                if "build_hook" in current_risk_ids and "build_hook" not in confirmed:
                    raise PluginInstallationError(
                        400,
                        {
                            "ok": False,
                            "error": "执行插件构建命令前需要明确确认",
                            "code": "build_hook_confirmation_required",
                            "required_risk_ids": ["build_hook"],
                        },
                    )
                self._previews.pop(preview_token)
                preview_owned = True
            else:
                if data is None:
                    self._fail(400, "缺少上传文件或 preview_token 无效")
                if len(data) > self._package_limits.max_upload_bytes:
                    self._fail(
                        400,
                        "插件包过大（>"
                        f"{self._package_limits.max_upload_bytes // (1024 * 1024)}MB）",
                    )
                archive_path = self._temporary_storage.write_archive(data)
                extract_dir = self._temporary_storage.create_directory()
                try:
                    extract_nmfp(
                        str(archive_path),
                        str(extract_dir),
                        password,
                        self._package_limits,
                    )
                except ValueError as error:
                    raise PluginInstallationError(
                        400,
                        {"ok": False, "error": "插件包未通过安全检查"},
                    ) from error
                root_path = self._plugin_root(extract_dir)
                plugin_kind, json_name = self._plugin_kind(root_path)
                meta = self._read_manifest(root_path / json_name)
                plugin_type = (
                    "trigger" if plugin_kind == "triggers" else "action"
                )
                valid, errors = validate_plugin_meta(meta, plugin_type)
                if not valid:
                    self._fail(400, "schema 校验失败: " + "; ".join(errors[:3]))
                risks = self.scan_install_risks(str(root_path), json_name, meta)
                if risks:
                    raise PluginInstallationError(
                        400,
                        {
                            "ok": False,
                            "error": "插件存在安全风险，请先预览后安装",
                            "risks": risks,
                        },
                    )

            package_name = meta.get("package_name", "")
            version_code = meta.get("version_code", 0)
            plugin_id = meta.get("id", root_path.name)
            if not self._safe_plugin_id(plugin_id):
                self._fail(400, "插件 id 含非法字符（禁止路径分隔符）")
            destination = self._paths.user_plugins_dir / plugin_kind / plugin_id
            expected_parent = self._paths.user_plugins_dir / plugin_kind
            if destination.parent != expected_parent:
                self._fail(400, "插件路径越界")

            collision = self._catalog.plugin_id_collision(plugin_kind, meta)
            if collision is not None:
                self._fail(409, "插件 id 已被其他包使用")

            existing = self._catalog.find_user_plugin_by_package(package_name)
            obsolete: tuple[Path, ...] = ()
            if existing:
                old_kind, old_id, old_meta = existing
                old_version = old_meta.get("version_code", 0)
                if not force and version_code < old_version:
                    self._fail(
                        400,
                        f"已安装更高版本 v{old_version}，如需降级请勾选「强制覆盖」后重试",
                    )
                old_destination = (
                    self._paths.user_plugins_dir / old_kind / old_id
                )
                if old_destination != destination:
                    obsolete = (old_destination,)

            self._build_hook(root_path, meta)
            built_meta = self._read_manifest(root_path / json_name)
            if built_meta != meta:
                self._fail(400, "插件构建命令不得修改 manifest")
            post_build_risks = [
                risk
                for risk in self.scan_install_risks(
                    str(root_path), json_name, built_meta
                )
                if risk.get("id") not in accepted_risk_ids
            ]
            if post_build_risks:
                raise PluginInstallationError(
                    400,
                    {
                        "ok": False,
                        "error": "构建产物引入了新的风险",
                        "risks": post_build_risks,
                    },
                )
            if (root_path / "public_key.pem").exists():
                counter_error = self._counter_sign_author_key(
                    root_path, signing_password
                )
                if counter_error:
                    self._fail(400, counter_error)

            def validate_staging(staging: Path) -> None:
                written_meta = self._read_manifest(staging / json_name)
                written_ok, written_errors = validate_plugin_meta(
                    written_meta, plugin_type
                )
                new_risks = [
                    risk
                    for risk in self.scan_install_risks(
                        str(staging), json_name, written_meta
                    )
                    if risk.get("id") not in accepted_risk_ids
                ]
                if not written_ok:
                    raise ValueError(
                        "安装后校验失败，已恢复旧版本: "
                        + "; ".join(written_errors[:3])
                    )
                if new_risks:
                    error = ValueError(
                        "安装后校验失败，已恢复旧版本: 构建产物引入了新的风险"
                    )
                    error.risks = new_risks
                    raise error
                from notmyfault.security.plugin_loader import validate_plugin_signature

                validate_plugin_signature(
                    staging, written_meta, "user", detect_security_mode()
                )

            try:
                backups = self._transaction.install_tree(
                    root_path,
                    destination,
                    validate=validate_staging,
                    keep_backup=True,
                    obsolete_destinations=obsolete,
                )
                kept_backups = set(backups)
                for old_backup in self._backups_for_package(package_name):
                    if old_backup not in kept_backups:
                        self._file_system.remove_tree(old_backup)
            except ValueError as error:
                body: Dict[str, Any] = {"ok": False, "error": str(error)}
                risks = getattr(error, "risks", None)
                if risks is not None:
                    body["risks"] = risks
                raise PluginInstallationError(400, body) from error
            except OSError as error:
                raise PluginInstallationError(
                    500,
                    {"ok": False, "error": "安装失败，已恢复旧版本"},
                ) from error
            return {
                "ok": True,
                "id": plugin_id,
                "type": plugin_kind,
                "package_name": package_name,
                "version_code": version_code,
                "restart_required": True,
                "backup_kept": bool(backups),
            }
        except ValueError as error:
            raise PluginInstallationError(
                400, {"ok": False, "error": str(error)}
            ) from error
        finally:
            if archive_path is not None:
                self._temporary_storage.remove_file(archive_path)
            if extract_dir is not None:
                self._file_system.discard_tree(extract_dir)
            if preview_token and not preview_owned and preview_token in self._previews:
                self._previews.discard(preview_token)

    @staticmethod
    def _parse_confirmed_risk_ids(value: Any) -> set[str]:
        if value in (None, ""):
            return set()
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError) as error:
                raise PluginInstallationError(
                    400,
                    {"ok": False, "error": "风险确认字段格式无效"},
                ) from error
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise PluginInstallationError(
                400,
                {"ok": False, "error": "风险确认字段格式无效"},
            )
        return set(value)

    def _read_manifest(
        self, manifest_path: Path, preview: bool = False
    ) -> Dict[str, Any]:
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            message = (
                "插件元数据无法读取，请重新预览"
                if preview
                else "插件元数据 JSON 损坏或缺失"
            )
            raise PluginInstallationError(
                400, {"ok": False, "error": message}
            ) from error
        if not isinstance(value, dict):
            self._fail(400, "插件元数据 JSON 损坏或缺失")
        return value

    def _counter_sign_author_key(
        self, plugin_dir: Path, password: str
    ) -> str | None:
        from notmyfault.security.signing import (
            counter_sign_author_key,
            load_private_key,
        )

        private_key_path = self._paths.plugin_private_key_file
        if not private_key_path.exists():
            return "缺少签名私钥，无法信任作者自签插件"
        encrypted = private_key_path.read_bytes()[:20].startswith(
            b"-----BEGIN ENCRYPTED"
        )
        if encrypted and not password:
            return "安装作者自签插件需要输入签名私钥密码"
        try:
            private_key = load_private_key(
                private_key_path, password=password or None
            )
            counter_sign_author_key(plugin_dir, private_key)
        except Exception as error:
            self._log_exception("counter_sign_author_key", error)
            return "签名私钥密码错误或副签失败"
        return None

    @staticmethod
    def _run_build_hook(root_path: Path, meta: Dict[str, Any]) -> None:
        build = meta.get("build")
        if not isinstance(build, dict):
            return
        commands = build.get("command") or []
        outputs = build.get("outputs") or []
        if not commands and not outputs:
            return
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    cwd=root_path,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=120,
                )
            except subprocess.TimeoutExpired as error:
                raise ValueError("插件构建命令执行超时（120 秒）") from error
            except OSError as error:
                raise ValueError("插件构建命令无法执行") from error
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()[-300:]
                raise ValueError("插件构建命令执行失败") from RuntimeError(
                    f"rc={result.returncode}; command={command!r}; output={detail}"
                )
        for relative_output in outputs:
            if not (root_path / relative_output).exists():
                raise ValueError("插件构建没有生成声明的产物")

    @staticmethod
    def _validate_generated_plugin(staging: Path, json_name: str) -> None:
        from notmyfault.plugin_cli import check_plugin

        report = check_plugin(staging)
        if not report["ok"]:
            raise PluginInstallationError(
                400,
                {
                    "ok": False,
                    "error": "生成插件未通过 plugin check",
                    "code": "plugin_check_failed",
                    "check": report,
                },
            )
        if not (staging / json_name).is_file():
            raise PluginInstallationError(
                400, {"ok": False, "error": "生成插件缺少元数据文件"}
            )

    @staticmethod
    def _log_exception(operation: str, error: Exception) -> None:
        engine_error(
            "api_request_failed",
            operation=operation,
            error=str(error),
        )

    def _update_diff(self, root_path: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        package_name = meta.get("package_name", "")
        incoming_version = meta.get("version_code", 0)
        existing = self._catalog.find_user_plugin_by_package(package_name)
        if not existing:
            return {"update": {"kind": "new", "package_name": package_name}}
        installed_kind, installed_id, installed_meta = existing
        installed_version = installed_meta.get("version_code", 0)
        if incoming_version > installed_version:
            update_kind = "upgrade"
        elif incoming_version < installed_version:
            update_kind = "downgrade"
        else:
            update_kind = "reinstall"
        classification = {
            "kind": update_kind,
            "package_name": package_name,
            "installed_id": installed_id,
            "installed_type": installed_kind,
            "installed_version": installed_meta.get("version", ""),
            "installed_version_code": installed_version,
            "incoming_version_code": incoming_version,
        }
        installed_dir = self._paths.user_plugins_dir / installed_kind / installed_id
        old_identity = self._signature_identity(str(installed_dir))
        new_identity = self._signature_identity(root_path)
        return {
            "update": classification,
            "permission_diff": self._list_diff(
                installed_meta.get("permissions"),
                meta.get("permissions"),
            ),
            "capability_diff": self._list_diff(
                installed_meta.get("requires_capabilities"),
                meta.get("requires_capabilities"),
            ),
            "signature_identity_changed": old_identity != new_identity,
            "signature_old": old_identity,
            "signature_new": new_identity,
        }

    @staticmethod
    def _plugin_root(extract_dir: Path) -> Path:
        directories = [item for item in extract_dir.iterdir() if item.is_dir()]
        if len(directories) == 1:
            return directories[0]
        if not directories:
            return extract_dir
        raise PluginInstallationError(
            400,
            {"ok": False, "error": "nmfp 根目录应恰好有一个插件文件夹"},
        )

    @staticmethod
    def _plugin_kind(root_path: Path) -> tuple[str, str]:
        if (root_path / "trigger.json").exists():
            return "triggers", "trigger.json"
        if (root_path / "action.json").exists():
            return "actions", "action.json"
        raise PluginInstallationError(
            400,
            {"ok": False, "error": "未找到 trigger.json 或 action.json"},
        )

    @staticmethod
    def _platform_compatible(meta: Dict[str, Any]) -> bool:
        entrypoints = meta.get("entrypoints", {})
        if entrypoints:
            return current_platform_name() in entrypoints
        platforms = meta.get("platforms", [])
        return not platforms or current_platform_name() in platforms

    @staticmethod
    def _list_diff(old_items: Any, new_items: Any) -> Dict[str, list[str]]:
        old = set(old_items or [])
        new = set(new_items or [])
        return {"added": sorted(new - old), "removed": sorted(old - new)}

    @staticmethod
    def _signature_identity(root_path: str) -> str:
        import hashlib

        kind = plugin_signature_kind(root_path, "user")
        if kind == "none":
            return "none"
        key_file = os.path.join(root_path, "public_key.pem")
        try:
            with open(key_file, "rb") as file:
                return "author:" + hashlib.sha256(file.read()).hexdigest()[:16]
        except OSError:
            return "trusted-local"

    def _load_config_for_update(self) -> Dict[str, Any]:
        if not os.path.exists(self._store.config_path):
            if not self._store.save_config({}):
                raise ConfigValidationError("无法创建配置文件")
        return self._store.load_verified_config()

    def _backups_for_package(self, package_name: str) -> list[Path]:
        backups = []
        for plugin_kind, json_name in (
            ("triggers", "trigger.json"),
            ("actions", "action.json"),
        ):
            plugin_root = self._paths.user_plugins_dir / plugin_kind
            try:
                candidates = list(plugin_root.iterdir())
            except OSError:
                continue
            for candidate in candidates:
                if not candidate.name.endswith(".nmf-backup"):
                    continue
                try:
                    meta = json.loads(
                        (candidate / json_name).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                if isinstance(meta, dict) and meta.get("package_name") == package_name:
                    backups.append(candidate)
        return backups

    @staticmethod
    def _safe_plugin_id(plugin_id: Any) -> bool:
        return isinstance(plugin_id, str) and is_valid_plugin_id(plugin_id)

    @staticmethod
    def _fail(status_code: int, message: str) -> None:
        raise PluginInstallationError(
            status_code,
            {"ok": False, "error": message},
        )
