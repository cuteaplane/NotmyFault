import math
import os
import re
import time


_ACTIONS = {"list", "list_monitors", "get_info", "bring_to_front", "minimize", "maximize",
            "restore", "show", "hide", "move", "resize", "move_resize", "center", "snap",
            "move_to_monitor", "pin", "unpin", "toggle_pin", "set_opacity", "close"}
_LAYOUTS = {"left": (0, 0, 1, 2), "right": (1, 0, 2, 2), "top": (0, 0, 2, 1),
            "bottom": (0, 1, 2, 2), "top_left": (0, 0, 1, 1), "top_right": (1, 0, 2, 1),
            "bottom_left": (0, 1, 1, 2), "bottom_right": (1, 1, 2, 2)}


def _backend():
    from .win32 import Windows
    return Windows()


def _number(params, name, default, low, high, *, integer=False):
    value = params.get(name, default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是数字") from None
    if isinstance(value, bool) or not math.isfinite(number) or not low <= number <= high or integer and not number.is_integer():
        raise ValueError(f"{name} 必须是 {low} 到 {high} 的{'整数' if integer else '数字'}")
    return int(number) if integer else number


def _options(params):
    action = params.get("action", "bring_to_front")
    target = params.get("target", "active")
    match = params.get("match", "unique")
    mode = params.get("match_mode", "contains")
    monitor = params.get("monitor", "current")
    if action not in _ACTIONS:
        raise ValueError(f"未知窗口操作: {action}")
    if target not in {"active", "title", "process", "pid", "class_name", "hwnd", "all"}:
        raise ValueError(f"未知窗口目标: {target}")
    if match not in {"unique", "first", "all"} or mode not in {"contains", "exact", "regex"}:
        raise ValueError("窗口匹配方式无效")
    if monitor not in {"current", "primary", "next", "previous", "index"}:
        raise ValueError("显示器选择无效")
    result = {"action": action, "target": target, "match": match, "match_mode": mode, "monitor": monitor,
              "include_hidden": params.get("include_hidden", False),
              "wait_seconds": _number(params, "wait_seconds", 0, 0, 60),
              "timeout_seconds": _number(params, "timeout_seconds", 5, 0.1, 60)}
    if not isinstance(result["include_hidden"], bool):
        raise ValueError("include_hidden 必须是布尔值")
    if action != "list_monitors":
        if target in {"title", "process", "class_name"}:
            name = {"title": "title", "process": "process_name", "class_name": "class_name"}[target]
            text = str(params.get(name, "") or "").strip()
            if not text:
                raise ValueError(f"选择 {target} 时必须填写 {name}")
            if mode == "regex" and target != "process":
                try:
                    result["pattern"] = re.compile(text, re.IGNORECASE)
                except re.error as error:
                    raise ValueError(f"窗口匹配正则无效: {error}") from error
            result["text"] = text
        elif target in {"pid", "hwnd"}:
            result[target] = _number(params, target, 0, 1, 2**32 - 1 if target == "pid" else 2**53 - 1, integer=True)
    if action in {"move", "move_resize"}:
        for name in ("x", "y"):
            result[name] = _number(params, name, 0, -(2**31), 2**31 - 1, integer=True)
    if action in {"resize", "move_resize"}:
        for name, default in (("width", 800), ("height", 600)):
            result[name] = _number(params, name, default, 1, 2**31 - 1, integer=True)
    if action in {"center", "snap", "move_to_monitor"} and monitor == "index":
        result["monitor_index"] = _number(params, "monitor_index", 1, 1, 64, integer=True)
    if action == "snap":
        result["layout"] = params.get("layout", "left")
        if result["layout"] not in _LAYOUTS:
            raise ValueError("分屏位置无效")
    if action == "set_opacity":
        result["opacity"] = _number(params, "opacity", 100, 0, 100, integer=True)
    return result


def validate_params(_meta, params):
    try:
        _options(params)
    except ValueError as error:
        return [str(error)]
    return []


def _matches(info, options):
    target = options["target"]
    if target in {"active", "hwnd", "all"}:
        return True
    if target == "pid":
        return info["pid"] == options["pid"]
    text = options["text"]
    if target == "process":
        name = text if text.lower().endswith(".exe") else text + ".exe"
        return info["process_name"].casefold() == name.casefold()
    value = info[target]
    if options["match_mode"] == "regex":
        return bool(options["pattern"].search(value))
    return value.casefold() == text.casefold() if options["match_mode"] == "exact" else text.casefold() in value.casefold()


def _select(backend, options):
    target = options["target"]
    handles = [backend.foreground()] if target == "active" else [options["hwnd"]] if target == "hwnd" else backend.handles()
    result = []
    for hwnd in handles:
        info = backend.info(hwnd)
        if info is None or not info["visible"] and not options["include_hidden"] and target != "hwnd":
            continue
        if _matches(info, options):
            result.append(info)
    if options["action"] == "list":
        return result
    if len(result) > 1 and options["match"] == "unique":
        raise RuntimeError(f"匹配到 {len(result)} 个窗口，请缩小匹配范围或选择首个／全部匹配窗口")
    return result[:1] if options["match"] == "first" else result


def _wait(predicate, deadline, cancellation, message):
    while True:
        if cancellation:
            cancellation.raise_if_cancelled()
        result = predicate()
        if result:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(message)
        if cancellation:
            cancellation.wait(min(0.05, remaining))
        else:
            time.sleep(min(0.05, remaining))


def _monitor(backend, hwnd, options):
    monitors = backend.monitors()
    if not monitors:
        raise RuntimeError("没有可用显示器")
    current_handle = backend.monitor_for(hwnd)
    current = next((i for i, item in enumerate(monitors) if item["handle"] == current_handle), 0)
    mode = options["monitor"]
    index = {"current": current, "primary": 0, "next": (current + 1) % len(monitors),
             "previous": (current - 1) % len(monitors), "index": options.get("monitor_index", 1) - 1}[mode]
    if index >= len(monitors):
        raise ValueError(f"显示器序号超出范围，当前有 {len(monitors)} 个显示器")
    return monitors[index]["work"]


def _operate(backend, before, options, deadline, cancellation):
    hwnd, action = before["hwnd"], options["action"]

    def await_info(check, message):
        def probe():
            current = backend.info(hwnd)
            if current is None:
                raise RuntimeError(f"窗口 {hwnd} 已不存在")
            return current if check(current) else None
        return _wait(probe, deadline, cancellation, message)

    if action == "get_info":
        return before
    if action == "close":
        backend.close(hwnd)
        _wait(lambda: not backend.exists(hwnd), deadline, cancellation, "窗口未在时限内关闭，可能正在等待保存确认")
        return {**before, "closed": True}
    if action in {"minimize", "maximize", "restore", "show", "hide"}:
        backend.show(hwnd, {"minimize": 6, "maximize": 3, "restore": 9, "show": 8, "hide": 0}[action])
        checks = {"minimize": lambda info: info["minimized"], "maximize": lambda info: info["maximized"],
                  "restore": lambda info: info["visible"] and not info["minimized"] and not info["maximized"],
                  "show": lambda info: info["visible"], "hide": lambda info: not info["visible"]}
        return await_info(checks[action], "窗口未在时限内完成状态切换")
    if action == "bring_to_front":
        if before["minimized"] or not before["visible"]:
            backend.show(hwnd, 9 if before["minimized"] else 8)
            await_info(lambda info: info["visible"] and not info["minimized"], "窗口未能恢复显示")
        backend.activate(hwnd)
        return await_info(lambda _: backend.foreground() == hwnd, "窗口未能切换到前台")
    if action in {"pin", "unpin", "toggle_pin"}:
        enabled = not before["topmost"] if action == "toggle_pin" else action == "pin"
        backend.pin(hwnd, enabled)
        return await_info(lambda info: info["topmost"] == enabled, "窗口置顶状态未生效")
    if action == "set_opacity":
        backend.opacity(hwnd, options["opacity"])
        return await_info(lambda info: info["opacity"] == options["opacity"], "窗口透明度未生效")
    if before["minimized"] or before["maximized"]:
        backend.show(hwnd, 9)
        before = await_info(lambda info: not info["minimized"] and not info["maximized"], "窗口未能恢复正常大小")
    rect = {key: before[key] for key in ("x", "y", "width", "height")}
    if action in {"move", "move_resize"}:
        rect.update(x=options["x"], y=options["y"])
    if action in {"resize", "move_resize"}:
        rect.update(width=options["width"], height=options["height"])
    if action in {"center", "snap", "move_to_monitor"}:
        work = _monitor(backend, hwnd, options)
        if action == "snap":
            left, top, right, bottom = _LAYOUTS[options["layout"]]
            x1, y1 = work["width"] * left // 2, work["height"] * top // 2
            x2, y2 = work["width"] * right // 2, work["height"] * bottom // 2
            rect = {"x": work["x"] + x1, "y": work["y"] + y1, "width": x2 - x1, "height": y2 - y1}
        else:
            rect["width"], rect["height"] = min(rect["width"], work["width"]), min(rect["height"], work["height"])
            rect["x"] = work["x"] + (work["width"] - rect["width"]) // 2
            rect["y"] = work["y"] + (work["height"] - rect["height"]) // 2
    backend.position(hwnd, rect)
    return await_info(lambda info: all(abs(info[key] - value) <= 1 for key, value in rect.items()),
                      "窗口未达到指定位置或大小，应用可能限制了窗口尺寸")


def run_with_context(_meta, params, context):
    if os.name != "nt":
        raise RuntimeError("window_control 仅支持 Windows")
    options = _options(params)
    cancellation = context.get("runtime", {}).get("cancellation")
    if cancellation:
        cancellation.raise_if_cancelled()
    backend = _backend()
    action = options["action"]
    if action == "list_monitors":
        monitors = backend.monitors()
        return {"action": action, "count": len(monitors), "hwnd": 0, "windows": [], "monitors": monitors}
    selected = _select(backend, options)
    if not selected and options["wait_seconds"]:
        selected = _wait(lambda: _select(backend, options), time.monotonic() + options["wait_seconds"],
                         cancellation, "等待窗口超时")
    if not selected and action != "list":
        raise RuntimeError("没有找到匹配的顶层窗口")
    if action == "bring_to_front" and len(selected) > 1:
        raise ValueError("前台激活只能选择一个窗口")
    deadline = time.monotonic() + options["timeout_seconds"]
    result = []
    for info in selected:
        if cancellation:
            cancellation.raise_if_cancelled()
        if time.monotonic() >= deadline:
            raise RuntimeError(f"窗口操作超过总时限，已完成 {len(result)}/{len(selected)} 个窗口")
        result.append(info if action == "list" else _operate(backend, info, options, deadline, cancellation))
    return {"action": action, "count": len(result), "hwnd": result[0]["hwnd"] if len(result) == 1 else 0,
            "windows": result, "monitors": []}


def run(meta, params):
    return run_with_context(meta, params, {})
