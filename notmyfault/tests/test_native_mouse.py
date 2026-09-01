"""屏幕坐标鼠标输入。"""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="屏幕坐标鼠标仅 Windows",
)

import notmyfault.native.mouse as mouse


SCREEN = {"left": -1920, "top": 0, "width": 3840, "height": 1080}


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeUser32:
    def __init__(self):
        self.positions = []
        self.contexts = []
        self.metrics = {
            mouse.SM_XVIRTUALSCREEN: -3840,
            mouse.SM_YVIRTUALSCREEN: 0,
            mouse.SM_CXVIRTUALSCREEN: 7680,
            mouse.SM_CYVIRTUALSCREEN: 2160,
        }
        self.SetCursorPos = FakeFunction(self._set_cursor_position)
        self.GetCursorPos = FakeFunction(self._get_cursor_position)
        self.GetSystemMetrics = FakeFunction(lambda metric: self.metrics[metric])
        self.SetThreadDpiAwarenessContext = FakeFunction(self._set_dpi_context)

    def _set_dpi_context(self, context):
        self.contexts.append(
            context.value if isinstance(context, mouse.ctypes.c_void_p) else context
        )
        return 1234

    def _set_cursor_position(self, x, y):
        self.positions.append((x, y))
        return 1

    @staticmethod
    def _get_cursor_position(pointer):
        pointer._obj.x = 123
        pointer._obj.y = 456
        return 1


def test_capture_cursor_position_includes_virtual_screen(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(mouse, "_user32", lambda: fake)
    monkeypatch.setattr(mouse, "_virtual_screen", lambda: SCREEN.copy())
    assert mouse.capture_cursor_position() == {
        "version": 1,
        "x": 123,
        "y": 456,
        "screen": SCREEN,
    }


def test_check_coordinate_uses_current_screen(monkeypatch):
    monkeypatch.setattr(mouse, "_virtual_screen", lambda: SCREEN.copy())
    assert mouse.check_coordinate({"x": -1, "y": 100})["found"] is True
    result = mouse.check_coordinate({"x": 5000, "y": 100})
    assert result["found"] is False
    assert "显示器布局" in result["error"]


def test_per_monitor_dpi_coordinates_cover_scaled_main_and_second_screen(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(mouse, "_user32", lambda: fake)
    screen = mouse.current_virtual_screen()
    assert screen == {
        "left": -3840,
        "top": 0,
        "width": 7680,
        "height": 2160,
    }
    assert mouse.check_coordinate({"x": 70, "y": 1900})["found"] is True
    assert mouse.check_coordinate({"x": -2000, "y": 1000})["found"] is True
    assert fake.contexts[0] == mouse.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2.value
    assert fake.contexts[1] == 1234


def test_perform_coordinate_moves_and_sends_click(monkeypatch):
    fake = FakeUser32()
    sent = []
    monkeypatch.setattr(mouse, "_user32", lambda: fake)
    monkeypatch.setattr(mouse, "_virtual_screen", lambda: SCREEN.copy())
    monkeypatch.setattr(
        mouse,
        "_send_mouse_flags",
        lambda flags, mouse_data=0: sent.append((flags, mouse_data)),
    )
    result = mouse.perform_coordinate({"x": 100, "y": 200}, "double_click")
    assert fake.positions == [(100, 200)]
    assert sent == [(mouse._OPERATIONS["double_click"], 0)]
    assert result == {
        "kind": "coordinate",
        "operation": "double_click",
        "x": 100,
        "y": 200,
    }


def test_perform_coordinate_rejects_changed_screen(monkeypatch):
    monkeypatch.setattr(mouse, "_virtual_screen", lambda: SCREEN.copy())
    with pytest.raises(ValueError, match="显示器布局"):
        mouse.perform_coordinate({"x": 5000, "y": 200}, "left_click")


@pytest.mark.parametrize("point", [{}, {"x": True, "y": 1}, {"x": 1, "y": 1.5}])
def test_invalid_coordinate_rejected(point):
    with pytest.raises(ValueError):
        mouse._point_coordinates(point)
