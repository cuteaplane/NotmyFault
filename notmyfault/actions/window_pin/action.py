"""窗口置顶动作：把当前或指定窗口设为置顶或取消置顶
Windows 用 SetWindowPos；Linux 用 wmctrl（仅 X11）
"""

import os
import shutil
import subprocess


def _run_linux(action: str, target: str, title: str) -> dict:
    if target == "title" and not title:
        raise ValueError("按标题匹配时必须填写窗口标题")
    wmctrl = shutil.which("wmctrl")
    if not wmctrl:
        raise RuntimeError("缺少窗口管理后端，请安装 wmctrl（仅 X11 支持）")
    if target == "title" and title:
        result = subprocess.run(
            [wmctrl, "-l"], capture_output=True, text=True, errors="replace", timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError("wmctrl -l 失败")
        wid = None
        for line in result.stdout.splitlines():
            if title.lower() in line.lower():
                wid = line.split()[0]
                break
        if not wid:
            raise RuntimeError(f"未找到标题包含 \"{title}\" 的窗口")
    else:
        result = subprocess.run(
            [wmctrl, "-l"], capture_output=True, text=True, errors="replace", timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError("wmctrl -l 失败")
        lines = result.stdout.strip().splitlines()
        if not lines:
            raise RuntimeError("没有可见窗口")
        wid = lines[0].split()[0]

    if action in ("toggle", "pin"):
        subprocess.run(
            [wmctrl, "-i", "-r", wid, "-b", "add,above"],
            check=True, timeout=5,
        )
        state = "pinned"
    else:
        subprocess.run(
            [wmctrl, "-i", "-r", wid, "-b", "remove,above"],
            check=True, timeout=5,
        )
        state = "unpinned"
    return {"state": state, "window_id": wid}


if os.name == "nt":
    from notmyfault.native import NATIVE_LOCK, WNDENUMPROC, typed_user32

    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008

    user32 = typed_user32()

    def _find_windows_by_title(keyword: str):
        import ctypes
        found = []
        def _callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if keyword.lower() in buf.value.lower():
                found.append(hwnd)
            return True
        user32.EnumWindows(WNDENUMPROC(_callback), 0)
        return found

    def _resolve_hwnd(params):
        target = params.get("target", "active")
        if target == "title":
            title = str(params.get("title", "") or "").strip()
            if not title:
                raise ValueError("按标题匹配时必须填写窗口标题")
            hwnds = _find_windows_by_title(title)
            if not hwnds:
                raise RuntimeError(f"未找到标题包含 \"{title}\" 的可见窗口")
            if len(hwnds) > 1:
                print(f"[Action:window_pin] 匹配到 {len(hwnds)} 个窗口，操作第一个")
            return hwnds[0]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise RuntimeError("没有活动窗口")
        return hwnd

    def _is_pinned(hwnd) -> bool:
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        return bool(style & WS_EX_TOPMOST)


def run(action_info, params):
    action = str(params.get("action", "toggle") or "toggle")
    if action not in ("toggle", "pin", "unpin"):
        raise ValueError(f"未知操作: {action!r}（可选: toggle/pin/unpin）")

    if os.name == "nt":
        with NATIVE_LOCK:
            hwnd = _resolve_hwnd(params)
            if action == "toggle":
                pin = not _is_pinned(hwnd)
            elif action == "pin":
                pin = True
            else:
                pin = False
            insert_after = HWND_TOPMOST if pin else HWND_NOTOPMOST
            ok = user32.SetWindowPos(
                hwnd, insert_after, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
            if not ok:
                raise RuntimeError("设置窗口置顶失败")
        state = "pinned" if pin else "unpinned"
        print(f"[Action:window_pin] {state} hwnd={hwnd}")
        return {"state": state, "hwnd": hwnd}
    else:
        target = params.get("target", "active")
        title = str(params.get("title", "") or "").strip()
        result = _run_linux(action, target, title)
        print(f"[Action:window_pin] {result['state']}")
        return result
