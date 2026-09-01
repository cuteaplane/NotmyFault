from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal

from notmyfault.config import ConfigValidationError, SignedConfigStore
from notmyfault.security.rule_approval import AdminRuleApprovalError


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
        return self._store.inspect_security(
            self._plugin_schema().get("actions", {})
        )

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
        return {"ok": True, "message": "配置已重新签名，引擎可正常启动"}
