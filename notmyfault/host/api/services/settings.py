from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Literal

from notmyfault.config import (
    ADMIN_AUTHORIZATION_MODES,
    ConfigValidationError,
    SignedConfigStore,
    get_admin_authorization_mode,
    get_admin_rule_key_verification,
)
from notmyfault.host.api.ports import EngineControlPort


@dataclass(frozen=True, slots=True)
class SettingsServiceError(Exception):
    kind: Literal["invalid", "integrity", "write"]
    message: str

    def __str__(self) -> str:
        return self.message


class SettingsService:
    def __init__(
        self,
        store: SignedConfigStore,
        engine: EngineControlPort,
        platform_name: str = os.name,
    ) -> None:
        self._store = store
        self._engine = engine
        self._platform_name = platform_name

    def security_status(self) -> Dict[str, Any]:
        return self._store.inspect_security()

    def approve_security(self) -> Dict[str, Any]:
        try:
            self._store.approve_current_files()
        except ConfigValidationError as error:
            raise SettingsServiceError("invalid", str(error)) from error
        return {"ok": True, "message": "配置已重新签名，引擎可正常启动"}

    def admin_authorization(self) -> Dict[str, Any]:
        config = self._load_config_for_read()
        selected = get_admin_authorization_mode(config)
        current_engine = self._engine.current_engine
        effective = (
            current_engine.admin_authorization_mode
            if current_engine is not None
            else None
        )
        supported = ["per_execution"]
        if self._platform_name == "nt":
            supported.append("engine_start")
        return {
            "mode": selected,
            "effective_mode": effective,
            "supported_modes": supported,
            "restart_required": bool(
                current_engine is not None and effective != selected
            ),
        }

    def update_admin_authorization(self, mode: Any) -> Dict[str, Any]:
        if mode not in ADMIN_AUTHORIZATION_MODES:
            raise SettingsServiceError("invalid", "管理员授权方式无效")
        if mode == "engine_start" and self._platform_name != "nt":
            raise SettingsServiceError(
                "invalid",
                "启动时一次授权目前只支持 Windows",
            )
        config = self._load_config_for_update()
        settings = config.get("settings")
        settings = dict(settings) if isinstance(settings, dict) else {}
        settings["admin_authorization_mode"] = mode
        config["settings"] = settings
        if not self._store.save_config(config):
            raise SettingsServiceError("write", "无法保存管理员授权方式")

        current_engine = self._engine.current_engine
        effective = (
            current_engine.admin_authorization_mode
            if current_engine is not None
            else None
        )
        return {
            "ok": True,
            "mode": mode,
            "effective_mode": effective,
            "restart_required": bool(
                current_engine is not None and effective != mode
            ),
        }

    def admin_rule_verification(self) -> Dict[str, Any]:
        return {
            "key_verification": get_admin_rule_key_verification(
                self._load_config_for_read()
            ),
        }

    def update_admin_rule_verification(self, value: Any) -> Dict[str, Any]:
        if not isinstance(value, bool):
            raise SettingsServiceError(
                "invalid",
                "key_verification 必须是布尔值",
            )
        config = self._load_config_for_update()
        settings = config.get("settings")
        settings = dict(settings) if isinstance(settings, dict) else {}
        settings["admin_rule_key_verification"] = value
        config["settings"] = settings
        if not self._store.save_config(config):
            raise SettingsServiceError("write", "无法保存验证设置")
        return {"ok": True, "key_verification": value}

    def _load_config_for_read(self) -> Dict[str, Any]:
        try:
            return self._store.load_verified_config()
        except ConfigValidationError:
            return {"rules": []}

    def _load_config_for_update(self) -> Dict[str, Any]:
        if not os.path.exists(self._store.config_path):
            if not self._store.save_config({}):
                raise SettingsServiceError("write", "无法创建配置文件")
        try:
            return self._store.load_verified_config()
        except ConfigValidationError as error:
            raise SettingsServiceError(
                "integrity",
                "配置未通过完整性校验",
            ) from error
