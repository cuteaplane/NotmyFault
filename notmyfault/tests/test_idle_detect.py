"""空闲时间触发器的 Windows tick 回绕计算"""

from notmyfault.triggers.idle_detect.trigger import _tick_delta_seconds


def test_tick_delta_wraps_at_dword_boundary():
    assert _tick_delta_seconds(1000, 900) == 0.1
    assert _tick_delta_seconds(1000, 0xFFFFFF00) == 1.256
