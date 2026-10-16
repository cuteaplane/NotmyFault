from typing import Any

from notmyfault.core.run_manager import TERMINAL_STATES

RUN_EVENT_SCHEMA_VERSION = 1
TERMINAL_STATUSES = TERMINAL_STATES


def is_terminal(status: Any) -> bool:
    return status in TERMINAL_STATUSES


def status_after(event_type: str, data: dict, current: str) -> str | None:
    if current in TERMINAL_STATUSES:
        return None
    terminal = event_type.removeprefix("run.")
    if terminal in TERMINAL_STATUSES:
        return terminal
    if event_type in {"run.triggered", "run.queued"} and current == "queued":
        return "queued"
    if event_type == "run.started" or event_type.startswith("run.step."):
        return "running"
    return None
