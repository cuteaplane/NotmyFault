"""rules.json 热重载，从 engine.py 的 _run 主循环拆出。

每秒看一眼 rules.json 的修改时间，变了就重载：先停旧触发器，
停了才换规则；换完取消延迟工作流、校验并重启触发器。
"""
import os
import sys
import traceback
from typing import Any, Callable, List

from notmyfault.config import ConfigValidationError
from notmyfault.core.logging import engine_error


class RulesHotReloader:
    """盯 rules.json 修改时间，变了就按固定顺序重载"""

    def __init__(
        self,
        rules_path_fn: Callable[[], str],
        load_rules_fn: Callable[[], List[Any]],
        stop_triggers_fn: Callable[..., bool],
        apply_rules_fn: Callable[[List[Any]], List[Any]],
        cancel_deferred_fn: Callable[[], None],
        validate_rules_fn: Callable[[], Any],
        start_triggers_fn: Callable[[List[Any]], int],
        diagnostics: Any,
        alert_cb: Callable[..., None],
    ) -> None:
        self._rules_path_fn = rules_path_fn
        self._load_rules_fn = load_rules_fn
        self._stop_triggers_fn = stop_triggers_fn
        self._apply_rules_fn = apply_rules_fn
        self._cancel_deferred_fn = cancel_deferred_fn
        self._validate_rules_fn = validate_rules_fn
        self._start_triggers_fn = start_triggers_fn
        self._diagnostics = diagnostics
        self._alert_cb = alert_cb
        self._rules_mtime = 0.0
        # 同一类错误只弹一次告警，原来是 engine 的 _hot_reload_error_reported
        self._error_reported = False

    def current_mtime(self) -> float:
        path = self._rules_path_fn()
        return os.path.getmtime(path) if os.path.exists(path) else 0

    def begin(self) -> None:
        """主循环开始前记录当前修改时间当基线"""
        # 热重载只盯 rules.json，开关插件等设置变更不再触发重载
        self._rules_mtime = self.current_mtime()
        self._error_reported = False

    def _restore_previous_rules(
        self, previous_rules: List[Any], new_mtime: float
    ) -> bool:
        try:
            self._stop_triggers_fn(timeout=30)
            self._apply_rules_fn(previous_rules)
            restored = self._start_triggers_fn(previous_rules)
            print(f"[Engine] 热加载失败，已恢复 {restored} 个旧触发器", file=sys.stderr)
            self._rules_mtime = new_mtime
            return True
        except Exception as error:
            print(f"[Engine] 恢复旧规则失败: {error}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            engine_error("hot_reload_restore_error", error=str(error))
            return False

    def check_once(self) -> None:
        """检查一次文件修改时间，变了就重载，主循环每秒调用"""
        new_mtime = self._rules_mtime
        previous_rules: List[Any] | None = None

        try:
            new_mtime = self.current_mtime()
            if new_mtime != self._rules_mtime:
                # 文件写入中被读取时可能出现临时 JSON 错误，下一轮继续尝试
                new_rules = self._load_rules_fn()

                # 旧触发器仍在运行时先等待退出，再决定是否加载新配置
                if not self._stop_triggers_fn(timeout=30):
                    message = "旧触发器未能在 30 秒内退出，已拒绝应用新配置"
                    if not self._error_reported:
                        self._diagnostics.inc_hot_reload_error()
                        engine_error("hot_reload_error", error=message)
                        print(f"[Engine] [!!] {message}", file=sys.stderr)
                        self._alert_cb("热重载被拒绝", message)
                        self._error_reported = True
                    return

                previous_rules = self._apply_rules_fn(new_rules)
                self._cancel_deferred_fn()

                print(
                    f"[Engine] 规则已热加载（{len(previous_rules)} -> {len(new_rules)} 条规则）"
                )
                self._error_reported = False
                self._validate_rules_fn()

                started = self._start_triggers_fn(new_rules)
                if started == 0:
                    print("[Engine] 热加载后无可用触发器，保持Engine运行")
                self._rules_mtime = new_mtime
        except ConfigValidationError as e:
            restored = True
            if previous_rules is not None:
                restored = self._restore_previous_rules(previous_rules, new_mtime)
            self._diagnostics.inc_hot_reload_error()
            engine_error("hot_reload_error", error=str(e))
            print(
                f"[Engine] 热加载规则校验失败: {e}",
                file=sys.stderr,
            )
            if not self._error_reported:
                self._error_reported = True
                self._alert_cb(
                    "规则校验失败",
                    "rules.json 未通过格式、签名或安全校验，热加载失败；请修改后重新保存",
                )
            if not restored:
                self._alert_cb(
                    "规则恢复失败",
                    "热加载失败且原有规则恢复失败，请重启引擎",
                )
            # 校验失败后记录当前修改时间，等文件再次保存再重试
            self._rules_mtime = new_mtime
        except OSError as e:
            print(
                f"[Engine] 读取规则文件失败: {e}",
                file=sys.stderr,
            )
        except Exception as error:
            print(
                "[Engine] 热加载规则失败:",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            if previous_rules is None:
                return
            restored = self._restore_previous_rules(previous_rules, new_mtime)
            self._diagnostics.inc_hot_reload_error()
            engine_error("hot_reload_error", error=str(error))
            if not restored:
                self._alert_cb("规则恢复失败", "热加载失败且原有规则恢复失败，请重启引擎")
            elif not self._error_reported:
                self._alert_cb("热加载失败", "新规则未能启动，已尝试恢复原有规则")
            if not self._error_reported:
                self._error_reported = True
