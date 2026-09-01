"""Windows 原生函数签名初始化"""

import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 提供 user32")
def test_typed_user32_only_writes_signatures_once(monkeypatch):
    import notmyfault.native as native

    class FakeFunction:
        def __init__(self):
            object.__setattr__(self, "writes", 0)

        def __setattr__(self, name, value):
            if name in ("argtypes", "restype"):
                object.__setattr__(self, "writes", self.writes + 1)
            object.__setattr__(self, name, value)

    names = (
        "IsWindowVisible",
        "GetWindowTextLengthW",
        "GetWindowTextW",
        "EnumWindows",
        "GetWindowThreadProcessId",
        "GetForegroundWindow",
        "GetWindowLongW",
        "SetWindowPos",
        "ShowWindow",
        "SetForegroundWindow",
        "PostMessageW",
    )
    fake_user32 = SimpleNamespace(**{name: FakeFunction() for name in names})
    monkeypatch.setattr(native, "_TYPED_USER32", None)
    monkeypatch.setattr(
        native.ctypes,
        "windll",
        SimpleNamespace(user32=fake_user32),
    )

    first = native.typed_user32()
    writes = sum(getattr(fake_user32, name).writes for name in names)
    second = native.typed_user32()

    assert first is fake_user32
    assert second is fake_user32
    assert sum(getattr(fake_user32, name).writes for name in names) == writes


def _selector():
    return {
        "version": 1,
        "window": {
            "process": "notepad.exe",
            "name": "无标题 - 记事本",
            "control_type": 50032,
        },
        "target": {
            "automation_id": "FileSave",
            "name": "保存",
            "control_type": 50000,
            "class_name": "Button",
        },
        "ancestors": [],
    }


def test_uia_selector_requires_reusable_target_identity():
    from notmyfault.native.uia import DesktopElementError, validate_selector

    assert validate_selector(_selector())["target"]["name"] == "保存"
    weak = _selector()
    weak["target"] = {"control_type": 50000}

    with pytest.raises(DesktopElementError, match="没有可复用"):
        validate_selector(weak)


def test_uia_selector_scoring_prefers_automation_id():
    from notmyfault.native import uia

    expected = _selector()["target"]
    exact = dict(expected)
    named_only = {
        "name": "保存",
        "control_type": 50000,
        "class_name": "Button",
    }

    assert uia._score(exact, expected) > uia._score(named_only, expected)


def test_uia_selector_rejects_ambiguous_matches():
    from notmyfault.native.uia import DesktopElementError, _best

    with pytest.raises(DesktopElementError, match="多个"):
        _best([(100, object()), (100, object())], "找不到", "找到多个控件")


def test_uia_text_write_requires_writable_value_pattern():
    from notmyfault.native.uia import _supports_text_write

    class Pattern:
        def __init__(self, read_only):
            self.CurrentIsReadOnly = read_only

        def QueryInterface(self, _interface):
            return self

    uia = SimpleNamespace(
        UIA_ValuePatternId=10002,
        IUIAutomationValuePattern=object(),
    )
    writable = SimpleNamespace(GetCurrentPattern=lambda _pattern_id: Pattern(False))
    read_only = SimpleNamespace(GetCurrentPattern=lambda _pattern_id: Pattern(True))

    assert _supports_text_write(writable, uia) is True
    assert _supports_text_write(read_only, uia) is False


def test_uia_wait_retries_missing_control_until_it_appears(monkeypatch):
    from notmyfault.native import uia

    @contextmanager
    def automation():
        yield object(), object()

    attempts = []

    def locate(_automation, _uia, _selector):
        attempts.append(True)
        if len(attempts) < 3:
            raise uia.DesktopElementError("element_not_found", "还没出现")
        return SimpleNamespace(CurrentBoundingRectangle=None)

    cancellation = SimpleNamespace(
        raise_if_cancelled=lambda: None,
        wait=lambda _seconds: False,
    )
    monkeypatch.setattr(uia, "_automation", automation)
    monkeypatch.setattr(uia, "_locate", locate)

    result = uia.wait_for_selector(_selector(), 1, cancellation)

    assert result["found"] is True
    assert len(attempts) == 3


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_uia_wait_rejects_nonfinite_timeout(timeout):
    from notmyfault.native import uia

    with pytest.raises(uia.DesktopElementError) as excinfo:
        uia.wait_for_selector(_selector(), timeout)

    assert excinfo.value.code == "invalid_timeout"


def test_uia_text_read_rejects_password_control(monkeypatch):
    from notmyfault.native import uia

    @contextmanager
    def automation():
        yield object(), object()

    monkeypatch.setattr(uia, "_automation", automation)
    monkeypatch.setattr(
        uia,
        "_locate",
        lambda _automation, _uia, _selector: SimpleNamespace(
            CurrentIsPassword=True
        ),
    )

    with pytest.raises(uia.DesktopElementError, match="密码输入框"):
        uia.read_selector_text(_selector())


def test_uia_selector_rejects_password_element_before_capture():
    from notmyfault.native.uia import DesktopElementError, _selector_from_element

    element = SimpleNamespace(CurrentIsPassword=True, CurrentProcessId=321)

    with pytest.raises(DesktopElementError, match="密码输入框"):
        _selector_from_element(SimpleNamespace(), SimpleNamespace(), element)


def test_uia_selector_requires_control_inside_window():
    from notmyfault.native.uia import DesktopElementError, _selector_from_element

    element = SimpleNamespace(
        CurrentIsPassword=False,
        CurrentControlType=50032,
        CurrentProcessId=321,
    )

    with pytest.raises(DesktopElementError, match="窗口里的"):
        _selector_from_element(SimpleNamespace(), SimpleNamespace(), element)


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 提供 GetAncestor")
def test_uia_selector_uses_native_root_when_control_view_omits_window(
    monkeypatch,
):
    from notmyfault.native import uia

    class GetAncestor:
        def __call__(self, hwnd, flag):
            assert hwnd == 123
            assert flag == 2
            return 456

    get_ancestor = GetAncestor()
    fake_user32 = SimpleNamespace(GetAncestor=get_ancestor)
    monkeypatch.setattr(
        uia.ctypes, "windll", SimpleNamespace(user32=fake_user32)
    )
    root = object()
    automation = SimpleNamespace(
        ElementFromHandle=lambda hwnd: root if hwnd == 456 else None
    )

    assert uia._native_root_window(
        automation, SimpleNamespace(CurrentNativeWindowHandle=123)
    ) is root


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 提供 WindowFromPoint")
def test_uia_selector_uses_window_under_capture_point(monkeypatch):
    from notmyfault.native import uia

    class NativeCall:
        def __init__(self, result):
            self.result = result
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.result

    fake_user32 = SimpleNamespace(
        WindowFromPoint=NativeCall(123),
        GetAncestor=NativeCall(456),
    )
    monkeypatch.setattr(
        uia.ctypes, "windll", SimpleNamespace(user32=fake_user32)
    )
    root = object()
    automation = SimpleNamespace(
        ElementFromHandle=lambda hwnd: root if hwnd == 456 else None
    )

    assert uia._native_root_window(
        automation,
        SimpleNamespace(CurrentNativeWindowHandle=0),
        point=(100, 200),
    ) is root


def test_uia_selector_uses_highest_non_desktop_parent():
    from notmyfault.native.uia import _tree_root_window

    panel = SimpleNamespace(CurrentControlType=50031)
    desktop = SimpleNamespace(CurrentControlType=50033)

    assert _tree_root_window([object(), panel, desktop]) is panel


def test_uia_invoke_clicks_element_bounds_when_pattern_is_missing(monkeypatch):
    from notmyfault.native import uia

    @contextmanager
    def automation():
        yield object(), SimpleNamespace(
            UIA_InvokePatternId=10000,
            IUIAutomationInvokePattern=object(),
        )

    class MissingPatternElement:
        CurrentIsPassword = False
        CurrentBoundingRectangle = SimpleNamespace(
            left=100, top=200, right=300, bottom=260
        )

        @staticmethod
        def GetCurrentPattern(_pattern_id):
            raise RuntimeError("pattern missing")

    calls = []
    monkeypatch.setattr(uia, "_automation", automation)
    monkeypatch.setattr(
        uia, "_locate", lambda _automation, _uia, _selector: MissingPatternElement()
    )
    monkeypatch.setattr(
        uia,
        "perform_coordinate",
        lambda point, operation, cancellation: calls.append((point, operation)),
    )

    result = uia.perform_selector(_selector(), "invoke")

    assert calls == [({"x": 200, "y": 230}, "left_click")]
    assert result["operation"] == "invoke"


@pytest.mark.parametrize(
    ("process", "expected"),
    [
        (
            "StartMenuExperienceHost.exe",
            [("left_click", {"x": 100, "y": 100})],
        ),
        ("notepad.exe", [("invoke", None)]),
    ],
)
def test_uia_invoke_uses_physical_click_only_for_shell_controls(
    monkeypatch, process, expected
):
    from notmyfault.native import uia

    @contextmanager
    def automation():
        yield object(), SimpleNamespace(
            UIA_InvokePatternId=10000,
            IUIAutomationInvokePattern=object(),
        )

    class Pattern:
        @staticmethod
        def QueryInterface(_interface):
            return Pattern()

        @staticmethod
        def Invoke():
            calls.append(("invoke", None))

    class StartMenuElement:
        CurrentIsPassword = False
        CurrentBoundingRectangle = SimpleNamespace(
            left=40, top=80, right=160, bottom=120
        )

        @staticmethod
        def GetCurrentPattern(_pattern_id):
            return Pattern()

    calls = []
    selector = _selector()
    selector["window"]["process"] = process
    monkeypatch.setattr(uia, "_automation", automation)
    monkeypatch.setattr(
        uia, "_locate", lambda _automation, _uia, _selector: StartMenuElement()
    )
    monkeypatch.setattr(
        uia,
        "perform_coordinate",
        lambda point, operation, cancellation: calls.append((operation, point)),
    )

    result = uia.perform_selector(selector, "invoke")

    assert calls == expected
    assert result["operation"] == "invoke"


@pytest.mark.skipif(sys.platform != "win32", reason="UIA 仅 Windows")
def test_uia_captures_foreground_window_signature(monkeypatch):
    from notmyfault import native
    from notmyfault.native import uia

    window = SimpleNamespace(
        CurrentAutomationId="TerminalWindow",
        CurrentName="终端",
        CurrentControlType=50032,
        CurrentClassName="CASCADIA_HOSTING_WINDOW_CLASS",
        CurrentFrameworkId="Win32",
        CurrentProcessId=123,
    )

    @contextmanager
    def automation():
        yield SimpleNamespace(ElementFromHandle=lambda hwnd: window), object()

    monkeypatch.setattr(
        native,
        "typed_user32",
        lambda: SimpleNamespace(GetForegroundWindow=lambda: 456),
        raising=False,
    )
    monkeypatch.setattr(uia, "_automation", automation)
    monkeypatch.setattr(uia, "_process_name", lambda process_id: "WindowsTerminal.exe")

    result = uia.capture_foreground_window()

    assert result["process"] == "WindowsTerminal.exe"
    assert result["name"] == "终端"


def test_uia_focuses_window_found_from_recorded_signature(monkeypatch):
    from notmyfault.native import uia

    target = object()
    focused = []

    @contextmanager
    def automation():
        yield object(), object()

    monkeypatch.setattr(uia, "_automation", automation)
    monkeypatch.setattr(uia, "_find_window", lambda *args: target)
    monkeypatch.setattr(uia, "_focus_window_element", lambda window: focused.append(window))

    result = uia.focus_window_signature({"process": "WindowsTerminal.exe"})

    assert focused == [target]
    assert result["focused"] is True
