from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Callable, Dict, Literal

from notmyfault.config import ConfigValidationError, SignedConfigStore
from notmyfault.security.rule_approval import AdminRuleApprovalError
from notmyfault.security.plugins import verify_plugin_sig
from notmyfault.security.security import (
    SecurityMode, detect_security_mode, verify_core_integrity,
)
from notmyfault.security.signing import verify_file


@dataclass(slots=True)
class SettingsServiceError(Exception):
    kind: Literal["invalid", "integrity", "write", "approval"]
    message: str
    code: str | None = None

    def __str__(self) -> str:
        return self.message


class SettingsService:
    def __init__(
        self,
        store: SignedConfigStore,
        plugin_schema: Callable[[], Dict[str, Any]] | None = None,
    ) -> None:
        self._store = store
        self._plugin_schema = plugin_schema or (
            lambda: {"triggers": {}, "actions": {}}
        )

    def security_status(self) -> Dict[str, Any]:
        result = self._store.inspect_security(
            self._plugin_schema().get("actions", {})
        )
        mode = detect_security_mode()
        paths = self._store.paths
        frozen = getattr(sys, "frozen", False)
        build_root = Path(sys._MEIPASS) if frozen else paths.project_root
        issues = []
        if not verify_file(str(build_root / "build.json")):
            issues.append({"path": "build.json", "reason": "构建信息缺失或签名无效"})
        checked = 0
        for kind, filename in (("actions", "action.json"), ("triggers", "trigger.json")):
            for metadata in sorted((paths.package_root / kind).glob(f"*/{filename}")):
                checked += 1
                if not verify_plugin_sig(str(metadata.parent), "builtin"):
                    issues.append({
                        "path": metadata.parent.relative_to(paths.package_root).as_posix(),
                        "reason": "内置插件签名缺失或无效",
                    })
        core_checked = mode == SecurityMode.STRICT and not frozen
        if core_checked:
            valid, files = verify_core_integrity()
            if not valid:
                issues.extend({"path": path, "reason": "核心文件完整性校验失败"} for path in files)
        result["installation"] = {
            "status": "invalid" if issues else "ok",
            "issues": issues,
            "checked_plugins": checked,
            "core_checked": core_checked,
        }
        result["security_mode"] = mode.value
        result["config_dir"] = str(paths.config_dir)
        return result

    def approve_security(self, admin_key_password: Any = None) -> Dict[str, Any]:
        try:
            self._store.approve_current_files(
                self._plugin_schema(),
                admin_key_password if isinstance(admin_key_password, str) else None,
            )
        except AdminRuleApprovalError as error:
            raise SettingsServiceError(
                "approval",
                str(error),
                code=error.code,
            ) from error
        except ConfigValidationError as error:
            raise SettingsServiceError(
                "invalid",
                str(error),
            ) from error
        return {"ok": True, "message": "设置与规则已重新签名"}
