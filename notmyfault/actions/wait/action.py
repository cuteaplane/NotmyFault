"""等待一段时间后再继续后续动作
秒与毫秒叠加；有取消对象时分段等待
"""
from notmyfault.plugin_api import action_values

import time

_MAX_SECONDS = 86400.0


def _total_seconds(params):
    try:
        seconds = float(params.get("seconds", 0) or 0)
    except (TypeError, ValueError) as error:
        raise ValueError(f"等待秒数必须是数字，实际: {params.get('seconds')!r}") from error
    try:
        milliseconds = float(params.get("milliseconds", 0) or 0)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"额外毫秒必须是数字，实际: {params.get('milliseconds')!r}"
        ) from error
    if seconds < 0 or milliseconds < 0:
        raise ValueError("等待时长不能为负数")
    total = seconds + milliseconds / 1000.0
    if total > _MAX_SECONDS:
        raise ValueError(f"等待时长不能超过 {_MAX_SECONDS:g} 秒")
    return total


def _sleep(total, cancellation):
    if total <= 0:
        return 0.0
    if cancellation is None:
        time.sleep(total)
        return total
    cancellation.raise_if_cancelled()
    started = time.monotonic()
    if cancellation.wait(total):
        cancellation.raise_if_cancelled()
    return time.monotonic() - started


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    params = action_values(params)
    total = _total_seconds(params)
    runtime = context.get("runtime", {})
    cancellation = runtime.get("cancellation")
    waited = _sleep(total, cancellation)
    print(f"[Action:wait] 等待 {waited:.3f}s（请求 {total:.3f}s）")
    return {"waited_seconds": waited}
