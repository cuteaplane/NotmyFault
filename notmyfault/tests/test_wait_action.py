"""wait 动作的时长计算与取消行为"""

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

from notmyfault.core.workflow import ActionCancellation, invoke_action


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "actions" / "wait"


def load_wait():
    meta = json.loads((PLUGIN_ROOT / "action.json").read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("wait_action_under_test", PLUGIN_ROOT / "action.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return meta, module


def execute(params, context=None):
    meta, module = load_wait()
    return invoke_action(module.run, module, meta, params, context or {})


def test_wait_adds_seconds_and_milliseconds():
    started = time.monotonic()
    result = execute({"seconds": 0, "milliseconds": 50})
    elapsed = time.monotonic() - started
    assert result["waited_seconds"] >= 0.04
    assert elapsed >= 0.04
    assert result["waited_seconds"] < 0.5


def test_wait_rejects_negative_or_oversized_duration():
    with pytest.raises(ValueError, match="负数"):
        execute({"seconds": -1, "milliseconds": 0})
    with pytest.raises(ValueError, match="86400"):
        execute({"seconds": 86401, "milliseconds": 0})


def test_wait_responds_to_cancellation():
    cancel_event = threading.Event()
    cancellation = ActionCancellation(cancel_event)

    def cancel_soon():
        time.sleep(0.05)
        cancel_event.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(Exception, match="取消|超时"):
        execute(
            {"seconds": 5, "milliseconds": 0},
            {"runtime": {"cancellation": cancellation}},
        )
    assert time.monotonic() - started < 2.0
