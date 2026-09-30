"""rules.json 热重载，从 engine.py 的 _run 主循环拆出。

每秒检查 rules.json 的修改时间，候选规则准备成功后再停止旧触发器。
"""
import os
import sys
import traceback
from typing import Any, Callable, List

from notmyfault.config import ConfigValidationError
from notmyfault.core.logging import engine_error

# 恢复旧规则要先停掉新触发器：第一次给足时间，重试时缩短，免得主循环被一直占住
_RESTORE_STOP_TIMEOUT = 30.0
_RESTORE_RETRY_STOP_TIMEOUT = 2.0
# 连续这么多次停不下来就放弃这次恢复，等用户重启引擎
_MAX_RESTORE_ATTEMPTS = 3


class RulesHotReloader:
    """盯 rules.json 修改时间，变了就按固定顺序重载"""

    def __init__(
        self,
        rules_path_fn: Callable[[], str],
        load_rules_fn: Callable[[], List[Any]],
        stop_triggers_fn: Callable[..., bool],
        prepare_rules_fn: Callable[[List[Any]], Any],
        apply_rules_fn: Callable[[Any], Any],
        start_triggers_fn: Callable[[Any], int],
        diagnostics: Any,
        alert_cb: Callable[..., None],
        accept_rules_fn: Callable[[Any], None] | None = None,
        current_rules_fn: Callable[[], Any] | None = None,
    ) -> None:
        self._rules_path_fn = rules_path_fn
        self._load_rules_fn = load_rules_fn
        self._stop_triggers_fn = stop_triggers_fn
        self._prepare_rules_fn = prepare_rules_fn
        self._apply_rules_fn = apply_rules_fn
        self._accept_rules_fn = accept_rules_fn or (lambda previous: None)
        self._current_rules_fn = current_rules_fn
        self._start_triggers_fn = start_triggers_fn
        self._diagnostics = diagnostics
        self._alert_cb = alert_cb
        self._rules_mtime = 0.0
        self._pending_restore: tuple[Any, float] | None = None
        self._restore_attempts = 0
        # 同一类错误只弹一次告警，原来是 engine 的 _hot_reload_error_reported
        self._error_reported = False

    def current_mtime(self) -> float:
        path = self._rules_path_fn()
        return os.path.getmtime(path) if os.path.exists(path) else 0

    def begin(self) -> None:
        """主循环开始前记下 rules.json 的修改时间"""
        # 热重载只盯 rules.json，开关插件等设置变更不再触发重载
        self._rules_mtime = self.current_mtime()
        self._error_reported = False
        self._restore_attempts = 0

    def _restore_previous_rules(
        self, previous_rules: Any, new_mtime: float
    ) -> bool:
        self._pending_restore = (previous_rules, new_mtime)
        self._restore_attempts += 1
        if self._restore_attempts > _MAX_RESTORE_ATTEMPTS:
            # 再试下去只是反复占住主循环，放弃这次恢复并推进时间戳，等文件下次变化
            print(
                f"[Engine] [!!] 旧触发器连续 {_MAX_RESTORE_ATTEMPTS} 次未能退出，"
                "已放弃恢复旧规则",
                file=sys.stderr,
            )
            engine_error("hot_reload_restore_giveup", attempts=_MAX_RESTORE_ATTEMPTS)
            self._pending_restore = None
            self._restore_attempts = 0
            self._rules_mtime = new_mtime
            self._alert_cb(
                "规则恢复失败",
                "热加载失败且旧触发器停不下来，已放弃恢复，请重启引擎",
            )
            return False
        timeout = (
            _RESTORE_STOP_TIMEOUT
            if self._restore_attempts == 1
            else _RESTORE_RETRY_STOP_TIMEOUT
        )
        try:
            if not self._stop_triggers_fn(timeout=timeout):
                engine_error("hot_reload_restore_error", error="新触发器尚未停止")
                return False
            self._apply_rules_fn(previous_rules)
            restored = self._start_triggers_fn(previous_rules)
            print(f"[Engine] 热加载失败，已恢复 {restored} 个旧触发器", file=sys.stderr)
            self._rules_mtime = new_mtime
            self._pending_restore = None
            self._restore_attempts = 0
            return True
        except Exception as error:
            print(f"[Engine] 恢复旧规则失败: {error}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            engine_error("hot_reload_restore_error", error=str(error))
            return False

    def _restart_current_triggers(self) -> bool:
        """换快照时就抛了异常，旧触发器已经被停掉，这里按当前规则把它们拉回来"""
        if self._current_rules_fn is None:
            return False
        try:
            started = self._start_triggers_fn(self._current_rules_fn())
            print(f"[Engine] 热加载失败，已按当前规则重启 {started} 个触发器", file=sys.stderr)
            return True
        except Exception as error:
            print(f"[Engine] 按当前规则重启触发器失败: {error}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            engine_error("hot_reload_restart_error", error=str(error))
            return False

    def check_once(self) -> None:
        """检查一次文件修改时间，变了就重载，主循环每秒调用"""
        if self._pending_restore is not None:
            self._restore_previous_rules(*self._pending_restore)
            return
        new_mtime = self._rules_mtime
        previous_rules: Any = None

        try:
            new_mtime = self.current_mtime()
            if new_mtime != self._rules_mtime:
                # 文件写入中被读取时可能出现临时 JSON 错误，下一轮继续尝试
                new_rules = self._prepare_rules_fn(self._load_rules_fn())

                # 触发器线程按配置指纹决定去留：配置没变的实例留着继续跑，不重启
                previous_rules = self._apply_rules_fn(new_rules)

                started = self._start_triggers_fn(new_rules)
                self._accept_rules_fn(previous_rules)
                print(
                    f"[Engine] 规则已热加载（{len(previous_rules)} -> {len(new_rules)} 条规则）"
                )
                self._error_reported = False
                if started == 0:
                    print("[Engine] 热加载后没有新启动的触发器，配置未变的实例继续运行")
                self._rules_mtime = new_mtime
                self._restore_attempts = 0
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
            if restored:
                self._rules_mtime = new_mtime
        except Exception as error:
            print(
                "[Engine] 热加载规则失败:",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            if previous_rules is None:
                restored = self._restart_current_triggers()
            else:
                restored = self._restore_previous_rules(previous_rules, new_mtime)
            self._diagnostics.inc_hot_reload_error()
            engine_error("hot_reload_error", error=str(error))
            if not restored:
                self._alert_cb("规则恢复失败", "热加载失败且原有规则恢复失败，请重启引擎")
            elif not self._error_reported:
                self._alert_cb("热加载失败", "新规则未能启动，已尝试恢复原有规则")
            if not self._error_reported:
                self._error_reported = True
            if restored:
                self._rules_mtime = new_mtime
