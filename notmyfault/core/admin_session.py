"""管理员授权会话管理，从 engine.py 拆出。

engine_start 模式下，引擎启动时扫一遍启用规则，算出哪些插件需要
admin 权限，向 sudo 请求一次本代会话；热重载后如果新规则引入了
admin 插件但会话没建起来，只提醒用户重启。
"""
import sys
from typing import Any, Callable, Dict, List

from notmyfault.core.logging import engine_warn
from notmyfault.core.rules import get_rule_admin_plugins


class AdminSessionManager:
    """算出规则需要哪些 admin 插件，负责本代授权会话的申请和复查"""

    def __init__(
        self,
        sudo: Any,
        engine_token: str,
        authorization_mode: str,
        rules_fn: Callable[[], List[Dict[str, Any]]],
        triggers_meta_fn: Callable[[], Dict[str, Dict[str, Any]]],
        actions_meta_fn: Callable[[], Dict[str, Dict[str, Any]]],
        alert_cb: Callable[..., None],
    ) -> None:
        # sudo 是引擎注入的模块对象，这里不直接 import
        self._sudo = sudo
        self._engine_token = engine_token
        # 授权模式在构造时取一次快照，引擎运行中不会变
        self._authorization_mode = authorization_mode
        self._rules_fn = rules_fn
        self._triggers_meta_fn = triggers_meta_fn
        self._actions_meta_fn = actions_meta_fn
        # 告警回调要支持 open_dashboard 关键字，engine._alert_user 就是这个签名
        self._alert_cb = alert_cb

    def required_admin_plugins(self) -> List[str]:
        """返回启用规则实际引用的管理员插件。"""
        triggers_meta = self._triggers_meta_fn()
        actions_meta = self._actions_meta_fn()
        required = set()
        for rule in self._rules_fn():
            if not isinstance(rule, dict) or rule.get("enabled") is False:
                continue
            required.update(get_rule_admin_plugins(
                rule, triggers_meta, actions_meta,
            ))
        return sorted(required)

    def authorize_at_startup(self) -> None:
        """在触发器启动前取得本代引擎的管理员授权。"""
        if self._authorization_mode != "engine_start":
            return
        required_admin_plugins = self.required_admin_plugins()
        if not required_admin_plugins:
            return

        names = "、".join(required_admin_plugins)
        print(f"[Engine] 请求本代管理员授权: {names}")
        try:
            self._sudo.start_admin_session(self._engine_token)
        except Exception as error:
            message = f"需要管理员权限的规则未启动：{error}"
            self._alert_cb("管理员授权未完成", message, open_dashboard=True)
            raise RuntimeError(message) from error

    def recheck_after_reload(self) -> None:
        """热重载后复查管理员授权，engine_start 会话无法在运行中补建时只提醒重启"""
        required_admin = self.required_admin_plugins()
        if (
            self._authorization_mode != "engine_start"
            or not required_admin
            or self._sudo.get_authorization_status()["session_active"]
        ):
            return
        names = "、".join(required_admin)
        message = (
            f"规则引用的管理员插件 {names} 尚未取得启动时授权，"
            "请重启引擎以完成管理员授权"
        )
        engine_warn(f"admin_session_missing: {message}")
        print(f"[Engine] [!!] {message}", file=sys.stderr)
        self._alert_cb("需要重启以完成管理员授权", message)
