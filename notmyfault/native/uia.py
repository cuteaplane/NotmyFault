"""Windows UI Automation 控件选择和回放"""

from __future__ import annotations

import ctypes
import os
import sys
import math
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

import psutil

from notmyfault.native import NATIVE_LOCK
from notmyfault.native.mouse import perform_coordinate


class DesktopElementError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_CONTROL_NAMES = {
    50000: "按钮",
    50001: "日历",
    50002: "复选框",
    50003: "下拉框",
    50004: "输入框",
    50005: "链接",
    50006: "图片",
    50007: "列表项",
    50008: "列表",
    50009: "菜单",
    50010: "菜单栏",
    50011: "菜单项",
    50012: "进度条",
    50013: "单选按钮",
    50014: "滚动条",
    50015: "滑块",
    50016: "微调框",
    50017: "状态栏",
    50018: "选项卡",
    50019: "选项卡项",
    50020: "文本",
    50021: "工具栏",
    50022: "提示",
    50023: "树",
    50024: "树项目",
    50025: "自定义控件",
    50026: "分组",
    50027: "缩略图",
    50028: "数据网格",
    50029: "数据项",
    50030: "文档",
    50031: "窗格",
    50032: "窗口",
    50033: "桌面",
}

_PHYSICAL_INVOKE_PROCESSES = {
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
}


def control_type_name(control_type: Any) -> str:
    try:
        numeric = int(control_type)
    except (TypeError, ValueError):
        return "控件"
    return _CONTROL_NAMES.get(numeric, f"控件 {numeric}")


def _text(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _current(element: Any, name: str, default: Any = "") -> Any:
    try:
        return getattr(element, name)
    except Exception:
        return default


def _process_name(process_id: Any) -> str:
    try:
        return psutil.Process(int(process_id)).name()[:160]
    except (psutil.Error, TypeError, ValueError):
        return ""


def _signature(element: Any, include_process: bool = False) -> Dict[str, Any]:
    signature = {
        "automation_id": _text(_current(element, "CurrentAutomationId")),
        "name": _text(_current(element, "CurrentName")),
        "control_type": int(_current(element, "CurrentControlType", 0) or 0),
        "class_name": _text(_current(element, "CurrentClassName")),
        "framework_id": _text(_current(element, "CurrentFrameworkId")),
    }
    if include_process:
        signature["process"] = _process_name(
            _current(element, "CurrentProcessId", 0)
        )
    return signature


def _compact_signature(signature: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in signature.items()
        if value not in ("", 0, None)
    }


def _requires_physical_invoke(selector: Dict[str, Any]) -> bool:
    window = selector.get("window")
    if not isinstance(window, dict):
        return False
    process = os.path.basename(_text(window.get("process"), 160)).casefold()
    return process in _PHYSICAL_INVOKE_PROCESSES


def _click_element_center(element: Any, cancellation: Any = None) -> None:
    bounds = _bounds(element)
    if not bounds.get("width") or not bounds.get("height"):
        raise DesktopElementError(
            "invoke_not_supported", "这个控件既不支持直接按下，也没有可点击的屏幕范围"
        )
    perform_coordinate(
        {
            "x": bounds["left"] + bounds["width"] // 2,
            "y": bounds["top"] + bounds["height"] // 2,
        },
        "left_click",
        cancellation,
    )


def _bounds(element: Any) -> Dict[str, int]:
    rect = _current(element, "CurrentBoundingRectangle", None)
    if rect is None:
        return {}
    try:
        return {
            "left": int(rect.left),
            "top": int(rect.top),
            "width": max(0, int(rect.right) - int(rect.left)),
            "height": max(0, int(rect.bottom) - int(rect.top)),
        }
    except (AttributeError, TypeError, ValueError):
        return {}


def _load_uia():
    if sys.platform != "win32":
        raise DesktopElementError(
            "platform_not_supported", "屏幕控件操作目前只支持 Windows"
        )
    try:
        import comtypes.client

        core_path = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "UIAutomationCore.dll",
        )
        comtypes.client.GetModule(core_path)
        from comtypes.gen import UIAutomationClient

        return comtypes.client, UIAutomationClient
    except Exception as exc:
        raise DesktopElementError(
            "uia_unavailable", "Windows UI Automation 不可用"
        ) from exc


@contextmanager
def _automation():
    import comtypes

    client, uia = _load_uia()
    comtypes.CoInitialize()
    try:
        automation = client.CreateObject(
            uia.CUIAutomation, interface=uia.IUIAutomation
        )
        yield automation, uia
    finally:
        comtypes.CoUninitialize()


def _parents(automation: Any, element: Any, limit: int = 16) -> list[Any]:
    walker = getattr(automation, "RawViewWalker", None)
    if walker is None:
        walker = automation.ControlViewWalker
    parents = []
    current = element
    for _index in range(limit):
        try:
            current = walker.GetParentElement(current)
        except Exception:
            break
        if not current:
            break
        parents.append(current)
        if int(_current(current, "CurrentControlType", 0) or 0) == 50033:
            break
    return parents


def _native_root_window(
    automation: Any,
    element: Any,
    parents: Iterable[Any] = (),
    point: tuple[int, int] | None = None,
) -> Any:
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    handles = []
    for candidate in (element, *parents):
        hwnd = int(_current(candidate, "CurrentNativeWindowHandle", 0) or 0)
        if hwnd and hwnd not in handles:
            handles.append(hwnd)
    with NATIVE_LOCK:
        user32 = ctypes.windll.user32
        if point is not None:
            user32.WindowFromPoint.argtypes = [wintypes.POINT]
            user32.WindowFromPoint.restype = wintypes.HWND
            point_hwnd = int(
                user32.WindowFromPoint(
                    wintypes.POINT(int(point[0]), int(point[1]))
                ) or 0
            )
            if point_hwnd and point_hwnd not in handles:
                handles.append(point_hwnd)
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        roots = []
        for hwnd in handles:
            root_hwnd = int(user32.GetAncestor(hwnd, 2) or hwnd)
            if root_hwnd not in roots:
                roots.append(root_hwnd)
    for root_hwnd in roots:
        try:
            window = automation.ElementFromHandle(root_hwnd)
        except Exception:
            continue
        if window:
            return window
    return None


def _tree_root_window(parents: Iterable[Any]) -> Any:
    for parent in reversed(list(parents)):
        control_type = int(_current(parent, "CurrentControlType", 0) or 0)
        if control_type != 50033:
            return parent
    return None


def _supports_pattern(element: Any, pattern_id: int) -> bool:
    try:
        return bool(element.GetCurrentPattern(pattern_id))
    except Exception:
        return False


def _supports_text_write(element: Any, uia: Any) -> bool:
    try:
        pattern = element.GetCurrentPattern(
            uia.UIA_ValuePatternId
        ).QueryInterface(uia.IUIAutomationValuePattern)
        return not bool(pattern.CurrentIsReadOnly)
    except Exception:
        return False


def _supports_text_read(element: Any, uia: Any) -> bool:
    if _supports_pattern(element, uia.UIA_ValuePatternId):
        return True
    return bool(_text(_current(element, "CurrentName")))


def _describe(selector: Dict[str, Any]) -> Dict[str, Any]:
    target = selector.get("target", {})
    window = selector.get("window", {})
    return {
        "control": target.get("name")
        or control_type_name(target.get("control_type")),
        "control_type": control_type_name(target.get("control_type")),
        "window": window.get("name") or "未命名窗口",
        "app": window.get("process") or "未知程序",
    }


def _selector_from_element(
    automation: Any,
    uia: Any,
    element: Any,
    point: tuple[int, int] | None = None,
) -> Dict[str, Any]:
    if bool(_current(element, "CurrentIsPassword", False)):
        raise DesktopElementError(
            "password_element", "密码输入框不能保存为录制控件"
        )
    control_type = int(_current(element, "CurrentControlType", 0) or 0)
    if control_type in (50032, 50033):
        raise DesktopElementError(
            "window_element", "请把鼠标移到窗口里的按钮、输入框或菜单项上"
        )
    process_id = int(_current(element, "CurrentProcessId", 0) or 0)
    if process_id == os.getpid():
        raise DesktopElementError(
            "own_window", "请把鼠标移到 NotmyFault 以外的程序控件上"
        )
    parents = _parents(automation, element)
    window = next(
        (
            parent
            for parent in parents
            if int(_current(parent, "CurrentControlType", 0) or 0) == 50032
        ),
        None,
    )
    if window is None:
        window = _native_root_window(automation, element, parents, point)
    if window is None:
        window = _tree_root_window(parents)
    if window is None:
        raise DesktopElementError("window_not_found", "目标程序没有提供可定位的窗口节点")
    ancestors = []
    for parent in parents:
        if parent is window:
            break
        signature = _compact_signature(_signature(parent))
        if signature:
            ancestors.append(signature)
        if len(ancestors) == 4:
            break
    bounds = _bounds(element)
    invoke_pattern = _supports_pattern(element, uia.UIA_InvokePatternId)
    selector = {
        "version": 1,
        "window": _compact_signature(_signature(window, include_process=True)),
        "target": _compact_signature(_signature(element)),
        "ancestors": ancestors,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "bounds": bounds,
        "capabilities": {
            "invoke": invoke_pattern
            or bool(bounds.get("width") and bounds.get("height")),
            "invoke_pattern": invoke_pattern,
            "focus": bool(_current(element, "CurrentIsKeyboardFocusable", False)),
            "set_text": _supports_text_write(element, uia),
            "read_text": _supports_text_read(element, uia),
        },
    }
    selector["display"] = _describe(selector)
    return selector


def capture_element_at(x: int, y: int) -> Dict[str, Any]:
    with _automation() as (automation, uia):
        point = uia.tagPOINT(int(x), int(y))
        element = automation.ElementFromPoint(point)
        if not element:
            raise DesktopElementError(
                "element_not_found", "鼠标位置下没有可读取的屏幕控件"
            )
        return _selector_from_element(automation, uia, element, (int(x), int(y)))


def capture_element_under_cursor() -> Dict[str, Any]:
    if sys.platform != "win32":
        raise DesktopElementError(
            "platform_not_supported", "屏幕控件操作目前只支持 Windows"
        )
    from ctypes import wintypes

    point = wintypes.POINT()
    with NATIVE_LOCK:
        user32 = ctypes.windll.user32
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        if not user32.GetCursorPos(ctypes.byref(point)):
            raise DesktopElementError(
                "cursor_unavailable", "无法读取当前鼠标位置"
            )
    return capture_element_at(point.x, point.y)


def capture_foreground_window() -> Dict[str, Any]:
    if sys.platform != "win32":
        raise DesktopElementError(
            "platform_not_supported", "前台窗口采集目前只支持 Windows"
        )
    from notmyfault.native import typed_user32

    with NATIVE_LOCK:
        hwnd = int(typed_user32().GetForegroundWindow() or 0)
    if not hwnd:
        raise DesktopElementError("window_not_found", "当前没有可记录的前台窗口")
    with _automation() as (automation, _uia):
        try:
            window = automation.ElementFromHandle(hwnd)
        except Exception as exc:
            raise DesktopElementError(
                "window_not_found", "无法读取当前前台窗口"
            ) from exc
        signature = _compact_signature(_signature(window, include_process=True))
    return validate_window_signature(signature)


def is_focused_password_control() -> bool:
    """返回当前焦点控件是否声明为密码输入控件，读取失败时按密码控件处理。"""
    if sys.platform != "win32":
        return False
    try:
        with _automation() as (automation, _uia):
            element = automation.GetFocusedElement()
            return bool(_current(element, "CurrentIsPassword", False))
    except Exception:
        return True


def validate_selector(selector: Any) -> Dict[str, Any]:
    if not isinstance(selector, dict) or selector.get("version") != 1:
        raise DesktopElementError(
            "invalid_selector", "屏幕控件信息格式无效，请重新选择"
        )
    window = selector.get("window")
    target = selector.get("target")
    if not isinstance(window, dict) or not isinstance(target, dict):
        raise DesktopElementError(
            "invalid_selector", "屏幕控件缺少窗口或控件特征，请重新选择"
        )
    control_type = target.get("control_type")
    if not isinstance(control_type, int) or isinstance(control_type, bool):
        raise DesktopElementError(
            "invalid_selector", "屏幕控件缺少控件类型，请重新选择"
        )
    if not any(target.get(key) for key in ("automation_id", "name", "class_name")):
        raise DesktopElementError(
            "weak_selector", "这个控件没有可复用的名称或标识，请选择更具体的控件"
        )
    for section in (window, target, *(selector.get("ancestors") or [])):
        if not isinstance(section, dict):
            raise DesktopElementError(
                "invalid_selector", "屏幕控件层级格式无效，请重新选择"
            )
        for value in section.values():
            if isinstance(value, str) and len(value) > 240:
                raise DesktopElementError(
                    "invalid_selector", "屏幕控件特征过长，请重新选择"
                )
    return selector


def validate_window_signature(window: Any) -> Dict[str, Any]:
    if not isinstance(window, dict):
        raise DesktopElementError("invalid_window", "录制的键盘窗口格式无效")
    if not any(window.get(key) for key in (
        "process", "automation_id", "name", "class_name"
    )):
        raise DesktopElementError("invalid_window", "录制的键盘窗口缺少可定位特征")
    for value in window.values():
        if isinstance(value, str) and len(value) > 240:
            raise DesktopElementError("invalid_window", "录制的键盘窗口特征过长")
    return window


def _elements(collection: Any, limit: int = 600) -> Iterable[Any]:
    length = min(int(collection.Length), limit)
    for index in range(length):
        yield collection.GetElement(index)


def _score(actual: Dict[str, Any], expected: Dict[str, Any]) -> int:
    weights = {
        "automation_id": 70,
        "name": 30,
        "control_type": 45,
        "class_name": 20,
        "framework_id": 8,
        "process": 55,
    }
    score = 0
    for key, weight in weights.items():
        value = expected.get(key)
        if value not in (None, "", 0) and actual.get(key) == value:
            score += weight
    return score


def _ancestor_score(
    automation: Any, element: Any, expected: list[Dict[str, Any]]
) -> int:
    if not expected:
        return 0
    actual = [
        _compact_signature(_signature(parent))
        for parent in _parents(automation, element, len(expected) + 1)
    ]
    return sum(
        min(_score(actual_item, expected_item), 24)
        for actual_item, expected_item in zip(actual, expected)
    )


def _best(
    candidates: list[tuple[int, Any]], missing: str, ambiguous: str
) -> Any:
    if not candidates:
        raise DesktopElementError("element_not_found", missing)
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_element = candidates[0]
    if best_score < 60:
        raise DesktopElementError("element_not_found", missing)
    if len(candidates) > 1 and candidates[1][0] == best_score:
        raise DesktopElementError("element_ambiguous", ambiguous)
    return best_element


def _find_window(automation: Any, uia: Any, expected: Dict[str, Any]) -> Any:
    root = automation.GetRootElement()
    collection = root.FindAll(
        uia.TreeScope_Children, automation.CreateTrueCondition()
    )
    candidates = []
    for element in _elements(collection):
        actual = _compact_signature(_signature(element, include_process=True))
        if expected.get("process") and actual.get("process") != expected["process"]:
            continue
        candidates.append((_score(actual, expected), element))
    return _best(
        candidates,
        "目标程序窗口没有打开",
        "找到多个相同程序窗口，无法确定要操作哪一个",
    )


def _find_target(
    automation: Any,
    uia: Any,
    window: Any,
    expected: Dict[str, Any],
    ancestors: list[Dict[str, Any]],
) -> Any:
    control_type = int(expected["control_type"])
    control_condition = automation.CreatePropertyCondition(
        uia.UIA_ControlTypePropertyId, control_type
    )
    conditions = [control_condition]
    if expected.get("automation_id"):
        conditions.append(
            automation.CreatePropertyCondition(
                uia.UIA_AutomationIdPropertyId, expected["automation_id"]
            )
        )
    condition = (
        automation.CreateAndCondition(conditions[0], conditions[1])
        if len(conditions) == 2
        else conditions[0]
    )
    collection = window.FindAll(uia.TreeScope_Descendants, condition)
    elements = list(_elements(collection))
    if not elements and expected.get("automation_id"):
        elements = list(_elements(
            window.FindAll(uia.TreeScope_Descendants, control_condition)
        ))
    candidates = []
    for element in elements:
        actual = _compact_signature(_signature(element))
        score = _score(actual, expected)
        score += _ancestor_score(automation, element, ancestors)
        candidates.append((score, element))
    return _best(
        candidates,
        "窗口已经打开，但找不到录制的控件",
        "窗口里有多个相同控件，无法确定要操作哪一个",
    )


def _locate(
    automation: Any, uia: Any, selector: Dict[str, Any]
) -> Any:
    validate_selector(selector)
    window = _find_window(automation, uia, selector["window"])
    return _find_target(
        automation,
        uia,
        window,
        selector["target"],
        selector.get("ancestors") or [],
    )


def check_selector(selector: Dict[str, Any]) -> Dict[str, Any]:
    with _automation() as (automation, uia):
        element = _locate(automation, uia, selector)
        current = _compact_signature(_signature(element))
        bounds = _bounds(element)
        invoke_pattern = _supports_pattern(element, uia.UIA_InvokePatternId)
        return {
            "ok": True,
            "display": {
                **_describe(selector),
                "control": current.get("name")
                or control_type_name(current.get("control_type")),
            },
            "bounds": bounds,
            "capabilities": {
                "invoke": invoke_pattern
                or bool(bounds.get("width") and bounds.get("height")),
                "invoke_pattern": invoke_pattern,
                "focus": bool(_current(element, "CurrentIsKeyboardFocusable", False)),
                "set_text": _supports_text_write(element, uia),
                "read_text": _supports_text_read(element, uia),
            },
        }


def perform_selector(
    selector: Dict[str, Any],
    operation: str = "invoke",
    cancellation: Any = None,
    text: str = "",
) -> Dict[str, Any]:
    if operation not in ("invoke", "focus", "set_text"):
        raise DesktopElementError(
            "invalid_operation", f"不支持的屏幕控件操作: {operation}"
        )
    if operation == "set_text" and not isinstance(text, str):
        raise DesktopElementError(
            "invalid_text", "写入屏幕控件的内容必须是文本"
        )
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    with _automation() as (automation, uia):
        element = _locate(automation, uia, selector)
        if bool(_current(element, "CurrentIsPassword", False)):
            raise DesktopElementError(
                "password_element", "密码输入框不能由录制动作操作"
            )
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if operation == "focus":
            element.SetFocus()
        elif operation == "set_text":
            try:
                pattern = element.GetCurrentPattern(
                    uia.UIA_ValuePatternId
                ).QueryInterface(uia.IUIAutomationValuePattern)
                if bool(pattern.CurrentIsReadOnly):
                    raise DesktopElementError(
                        "read_only_element", "这个控件是只读的，不能写入文本"
                    )
                pattern.SetValue(text)
            except DesktopElementError:
                raise
            except Exception as exc:
                raise DesktopElementError(
                    "set_text_not_supported", "这个控件不支持直接写入文本"
                ) from exc
        elif _requires_physical_invoke(selector):
            _click_element_center(element, cancellation)
        else:
            try:
                pattern = element.GetCurrentPattern(
                    uia.UIA_InvokePatternId
                ).QueryInterface(uia.IUIAutomationInvokePattern)
                pattern.Invoke()
            except Exception:
                _click_element_center(element, cancellation)
        return {
            "operation": operation,
            "display": _describe(selector),
        }


def wait_for_selector(
    selector: Dict[str, Any],
    timeout_seconds: float = 30,
    cancellation: Any = None,
) -> Dict[str, Any]:
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise DesktopElementError(
            "invalid_timeout", "等待时间必须是 1 到 600 秒"
        ) from exc
    if not math.isfinite(timeout) or timeout < 1 or timeout > 600:
        raise DesktopElementError(
            "invalid_timeout", "等待时间必须是 1 到 600 秒"
        )
    deadline = time.monotonic() + timeout
    with _automation() as (automation, uia):
        while True:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            try:
                element = _locate(automation, uia, selector)
                return {
                    "found": True,
                    "display": _describe(selector),
                    "bounds": _bounds(element),
                }
            except DesktopElementError as exc:
                if exc.code != "element_not_found":
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DesktopElementError(
                    "wait_timeout", "等待结束，仍然找不到这个控件"
                )
            pause = min(0.25, remaining)
            if cancellation is not None:
                cancellation.wait(pause)
            else:
                time.sleep(pause)


def focus_selector_window(
    selector: Dict[str, Any], cancellation: Any = None
) -> Dict[str, Any]:
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    with _automation() as (automation, uia):
        validate_selector(selector)
        window = _find_window(automation, uia, selector["window"])
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        _focus_window_element(window)
    return {"focused": True, "display": _describe(selector)}


def _focus_window_element(window: Any) -> None:
    focused = False
    focus_error = None
    try:
        window.SetFocus()
    except Exception as exc:
        focus_error = exc
    else:
        focused = True
    hwnd = int(_current(window, "CurrentNativeWindowHandle", 0) or 0)
    if hwnd and sys.platform == "win32":
        from notmyfault.native import typed_user32

        with NATIVE_LOCK:
            user32 = typed_user32()
            user32.SetForegroundWindow(hwnd)
            focused = int(user32.GetForegroundWindow() or 0) == hwnd
    if not focused:
        raise DesktopElementError(
            "window_focus_failed", "无法切换到录制的窗口"
        ) from focus_error


def focus_window_signature(
    window_signature: Dict[str, Any],
    cancellation: Any = None,
    timeout_seconds: float = 5.0,
) -> Dict[str, Any]:
    expected = validate_window_signature(window_signature)
    deadline = time.monotonic() + min(max(float(timeout_seconds), 0.1), 30.0)
    with _automation() as (automation, uia):
        while True:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            try:
                window = _find_window(automation, uia, expected)
                _focus_window_element(window)
                return {"focused": True, "window": dict(expected)}
            except DesktopElementError as exc:
                if (
                    exc.code not in ("element_not_found", "window_focus_failed")
                    or time.monotonic() >= deadline
                ):
                    raise
            pause = min(0.2, deadline - time.monotonic())
            if cancellation is not None:
                cancellation.wait(pause)
            else:
                time.sleep(pause)


def read_selector_text(
    selector: Dict[str, Any], cancellation: Any = None
) -> Dict[str, Any]:
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    with _automation() as (automation, uia):
        element = _locate(automation, uia, selector)
        if bool(_current(element, "CurrentIsPassword", False)):
            raise DesktopElementError(
                "password_element", "密码输入框的内容不能被录制动作读取"
            )
        value = None
        try:
            pattern = element.GetCurrentPattern(
                uia.UIA_ValuePatternId
            ).QueryInterface(uia.IUIAutomationValuePattern)
            value = pattern.CurrentValue
        except Exception:
            value = _current(element, "CurrentName", None)
        if value is None:
            raise DesktopElementError(
                "text_not_supported", "这个控件没有可读取的文本"
            )
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return {
            "text": _text(value, limit=20000),
            "display": _describe(selector),
        }
