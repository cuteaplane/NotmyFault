"""run 从触发到结束的状态约束

一个 run 只走到一个终点：succeeded / failed / cancelled / dropped / replaced。
queued 是排队还没跑，running 是执行中，deferred 是等前置条件过会儿重试。

终点定下来之后这个 run 的事件就不再改状态。写入侧 WorkflowExecutor 的
_finish_run 只放行第一个终态事件，回放侧 run_history 遇到终态后的任何事件
直接跳过，所以迟到的重复 complete / cancel / resume 改不掉已定的终点。

两个终态事件的分工：workflow_failed 是引擎没把 run 走下去，比如数据绑定
解析失败或动态参数被拦截；workflow_completed 是动作序列走完了，status 是
succeeded / failed / cancelled。UI 和统计以先到的一个为准。

步骤状态的名字在两处不一样：context["steps"] 和 action_executed 事件里用
ok / failed / cancelled / timed_out / skipped，检查功能取值时只认 ok；
run_history 回放时把 ok 改写成 succeeded，其余原样保留。这个改写只在
run_history._apply_event 做一次，别处不要再映射。
"""

from typing import Any, Dict

RUN_EVENT_SCHEMA_VERSION = 1

TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "dropped", "replaced"}
)

_TERMINAL_EVENTS = frozenset(
    {"workflow_failed", "workflow_completed", "run_dropped", "run_replaced"}
)

# 动作类事件不改 run 状态，但 deferred 之后再来说明重试已把 run 拉回执行；
# queued 的 run 派发后也靠第一个动作事件把状态翻回 running
_RESUME_SIGNALS = frozenset(
    {
        "action_executed",
        "action_skipped",
        "action_cancelled",
        "action_timed_out",
        "error",
        "test_assertions_completed",
    }
)


def is_terminal(status: Any) -> bool:
    return status in TERMINAL_STATUSES


def status_after(event_type: str, data: Dict[str, Any], current: str) -> str | None:
    """返回这个事件把 run 推进到什么状态；None 表示状态不变或该忽略

    已经到终点的 run 对后续事件一律返回 None。
    """
    if current in TERMINAL_STATUSES:
        return None
    if event_type == "rule_triggered":
        return "running"
    if event_type == "run_queued":
        return "queued"
    if event_type == "workflow_deferred":
        return "deferred"
    if event_type == "run_dropped":
        return "dropped"
    if event_type == "run_replaced":
        return "replaced"
    if event_type == "workflow_failed":
        return "failed"
    if event_type == "workflow_completed":
        status = data.get("status", "succeeded")
        return status if status in TERMINAL_STATUSES else "succeeded"
    if event_type in _RESUME_SIGNALS and current in ("deferred", "queued"):
        return "running"
    return None
