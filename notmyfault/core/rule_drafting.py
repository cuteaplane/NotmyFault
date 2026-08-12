"""把简短中文描述整理成待检查的规则草稿"""

from __future__ import annotations

import re
from typing import Any, Dict


_TRIGGER_INTENTS = (
    ("usb_insert", ("u盘", "usb设备", "usb 插入", "插入usb")),
    ("wifi_network", ("连上wifi", "连接wifi", "断开wifi", "wifi 网络", "wifi网络")),
    ("audio_device", ("音频设备", "播放设备", "录音设备", "耳机切换")),
    ("battery_level", ("电池电量", "电量低于", "电量高于", "电量不足")),
    ("folder_monitor", ("文件夹", "目录变化", "新文件", "文件出现")),
    ("clipboard", ("剪贴板变化", "复制内容", "复制文字")),
    ("idle_detect", ("电脑空闲", "一段时间没操作", "没有操作")),
    ("process_state", ("程序启动", "程序关闭", "进程启动", "进程退出")),
    ("window_title", ("窗口出现", "窗口打开", "窗口标题")),
    ("network_status", ("网络断开", "网络连接", "断网", "恢复联网")),
    ("power_state", ("插上电源", "拔掉电源", "电池供电", "休眠恢复")),
    ("session_lock", ("锁屏时", "解锁时", "电脑解锁", "电脑锁定")),
    ("system_resource", ("cpu超过", "cpu 高于", "内存超过", "磁盘超过", "网络占用")),
    ("system_startup", ("开机后", "引擎启动", "notmyfault启动")),
    ("cron_schedule", ("每隔", "每周", "工作日", "计划任务")),
    ("time_schedule", ("每天", "每晚", "定时", "固定时间", "到点")),
    ("hotkey", ("快捷键", "组合键")),
    ("manual", ("手动运行", "我点一下", "手动开始")),
)

_ACTION_INTENTS = (
    ("notify", ("提醒我", "通知我", "显示通知", "弹个通知")),
    ("uia_wait", ("等待控件", "等按钮出现", "等输入框出现")),
    ("uia_focus_window", ("切换到窗口", "激活窗口", "回到窗口")),
    ("uia_read_text", ("读取控件文字", "读取控件文本", "读取输入框")),
    ("file_operation", ("备份文件", "复制文件", "移动文件", "删除文件")),
    ("append_text", ("追加到文件", "写入日志", "记录到文件", "记到文件")),
    ("launch_program", ("启动程序", "打开程序", "运行程序")),
    ("open_url", ("打开网页", "访问网站", "打开网址", "打开链接")),
    ("screenshot", ("截图", "截屏", "截个图")),
    ("set_volume", ("设置音量", "调节音量", "把音量")),
    ("clipboard_set", ("写入剪贴板", "设置剪贴板")),
    ("clipboard_clear", ("清空剪贴板", "删掉剪贴板内容")),
    ("bluetooth_toggle", ("打开蓝牙", "关闭蓝牙", "开关蓝牙")),
    ("create_shortcut", ("创建快捷方式", "新建快捷方式")),
    ("display_control", ("关闭显示器", "打开显示器", "设置亮度", "调节亮度")),
    ("http_request", ("发送http请求", "调用接口", "请求接口")),
    ("kill_process", ("结束进程", "终止进程", "关闭进程")),
    ("lock_screen", ("锁定屏幕", "立即锁屏", "锁住电脑")),
    ("media_control", ("播放音乐", "暂停音乐", "下一首", "上一首", "暂停播放")),
    ("power_plan", ("切换电源计划", "高性能模式", "节能模式")),
    ("run_powershell", ("执行powershell", "运行powershell", "powershell命令")),
    ("send_keys", ("发送按键", "输入文字", "按组合键")),
    ("shutdown_system", ("关机", "重启电脑", "注销电脑")),
    ("text_to_speech", ("朗读文字", "语音播报", "念出来")),
    ("uia_control", ("点击按钮", "按下按钮", "填写输入框", "操作控件")),
    ("wallpaper", ("更换壁纸", "设置壁纸", "换桌面背景")),
    ("window_pin", ("窗口置顶", "保持窗口最前", "取消置顶")),
)

_PARAM_HINTS = {
    ("trigger", "folder_monitor"): ("folder_path",),
    ("trigger", "hotkey"): ("hotkey",),
    ("action", "notify"): ("message",),
    ("action", "file_operation"): ("source", "destination"),
    ("action", "launch_program"): ("path",),
    ("action", "open_url"): ("url",),
    ("action", "uia_control"): ("target",),
}


def _text(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _default_params(meta: Dict[str, Any]) -> Dict[str, Any]:
    result = {}
    for param in meta.get("params", []):
        if not isinstance(param, dict) or not param.get("name"):
            continue
        if "default" in param:
            value = param["default"]
        elif param.get("type") == "bool":
            value = False
        elif param.get("type") == "uia_selector":
            value = {}
        else:
            value = ""
        result[param["name"]] = value
    return result


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", "", _text(value).casefold())


def _match_intent(text: str, intents: tuple) -> str:
    lowered = _normalized(text)
    for plugin_id, phrases in intents:
        if any(_normalized(phrase) in lowered for phrase in phrases):
            return plugin_id
    return ""


def _match_plugin_name(text: str, plugins: Dict[str, Any]) -> str:
    lowered = _normalized(text)
    for plugin_id, meta in plugins.items():
        name = _text(meta.get("name") if isinstance(meta, dict) else "")
        if name and _normalized(name) in lowered:
            return plugin_id
    return ""


def _match_action_intents(
    text: str,
    plugins: Dict[str, Any],
) -> list[str]:
    lowered = _normalized(text)
    matches = []
    for plugin_id, phrases in _ACTION_INTENTS:
        positions = [
            lowered.find(_normalized(phrase))
            for phrase in phrases
            if _normalized(phrase) in lowered
        ]
        if positions:
            matches.append((min(positions), plugin_id))
    for plugin_id, meta in plugins.items():
        name = _text(meta.get("name") if isinstance(meta, dict) else "")
        position = lowered.find(_normalized(name)) if name else -1
        if position >= 0:
            matches.append((position, plugin_id))
    result = []
    for _position, plugin_id in sorted(matches, key=lambda item: item[0]):
        if plugin_id not in result:
            result.append(plugin_id)
    return result


def _extract_time(text: str) -> str:
    match = re.search(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", text)
    if not match:
        return ""
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _extract_notification(text: str) -> str:
    match = re.search(r"(?:提醒我|通知我|显示通知)[：:，,\s]*(.+)$", text)
    return _text(match.group(1), 500) if match else ""


def _plugin_name(schema: Dict[str, Any], kind: str, plugin_id: str) -> str:
    group = "triggers" if kind == "trigger" else "actions"
    return _text(schema.get(group, {}).get(plugin_id, {}).get("name")) or plugin_id


def _missing_params(
    kind: str,
    plugin_id: str,
    params: Dict[str, Any],
    meta: Dict[str, Any],
) -> list[str]:
    names = list(_PARAM_HINTS.get((kind, plugin_id), ()))
    if kind == "action" and plugin_id == "file_operation":
        if params.get("operation") == "delete" and "destination" in names:
            names.remove("destination")
    names.extend(
        param.get("name")
        for param in meta.get("params", [])
        if isinstance(param, dict) and param.get("required") is True
    )
    definitions = {
        item.get("name"): item
        for item in meta.get("params", [])
        if isinstance(item, dict)
    }
    result = []
    for name in dict.fromkeys(names):
        value = params.get(name)
        if value not in (None, "", [], {}):
            continue
        definition = definitions.get(name, {})
        result.append(_text(definition.get("label") or name, 100))
    return result


def draft_rule_from_text(
    description: Any,
    schema: Dict[str, Any],
) -> Dict[str, Any]:
    text = _text(description)
    if not text:
        return {
            "ok": False,
            "code": "empty_description",
            "error": "请先说说什么时候开始、接着要做什么",
        }
    if len(str(description or "")) > 2000:
        return {
            "ok": False,
            "code": "description_too_long",
            "error": "描述不能超过 2000 个字符",
        }

    triggers = schema.get("triggers", {}) if isinstance(schema, dict) else {}
    actions = schema.get("actions", {}) if isinstance(schema, dict) else {}
    trigger_type = (
        _match_intent(text, _TRIGGER_INTENTS)
        or _match_plugin_name(text, triggers)
    )
    action_types = _match_action_intents(text, actions)
    assumptions = []
    missing = []

    if not trigger_type and action_types:
        trigger_type = "manual"
        assumptions.append("没有识别到开始条件，先按手动运行起草")
    if not trigger_type:
        missing.append("什么时候开始")
    if not action_types:
        missing.append("开始以后做什么")

    unavailable = []
    if trigger_type and trigger_type not in triggers:
        unavailable.append(_plugin_name(schema, "trigger", trigger_type))
    for action_type in action_types:
        if action_type not in actions:
            unavailable.append(_plugin_name(schema, "action", action_type))

    draft = None
    if trigger_type and trigger_type in triggers:
        trigger_meta = triggers[trigger_type]
        trigger_params = _default_params(trigger_meta)
        if trigger_type == "time_schedule":
            clock = _extract_time(text)
            if clock:
                trigger_params["time"] = clock
            else:
                missing.append("每天几点开始")
        missing.extend(
            f"{_plugin_name(schema, 'trigger', trigger_type)}：{label}"
            for label in _missing_params(
                "trigger", trigger_type, trigger_params, trigger_meta
            )
        )

        draft_actions = []
        for action_type in action_types:
            if action_type not in actions:
                continue
            action_meta = actions[action_type]
            action_params = _default_params(action_meta)
            if action_type == "notify":
                message = _extract_notification(text)
                if message:
                    action_params["message"] = message
            if action_type == "file_operation" and "operation" in action_params:
                if "移动文件" in text:
                    action_params["operation"] = "move"
                elif "删除文件" in text:
                    action_params["operation"] = "delete"
                else:
                    action_params["operation"] = "copy"
            missing.extend(
                f"{_plugin_name(schema, 'action', action_type)}：{label}"
                for label in _missing_params(
                    "action", action_type, action_params, action_meta
                )
            )
            draft_actions.append({"type": action_type, "params": action_params})

        name = re.sub(r"\s+", " ", text).strip("，。！？,.!? ")
        if len(name) > 36:
            name = name[:35] + "…"
        draft = {
            "name": name or "自然语言草稿",
            "folder": "未分类",
            "event": {"type": trigger_type, "params": trigger_params},
            "actions": draft_actions,
        }

    return {
        "ok": True,
        "source": "local",
        "draft": draft,
        "interpretation": {
            "trigger": {
                "type": trigger_type,
                "name": _plugin_name(schema, "trigger", trigger_type)
                if trigger_type else "",
            },
            "actions": [{
                "type": action_type,
                "name": _plugin_name(schema, "action", action_type),
            } for action_type in action_types],
        },
        "assumptions": assumptions,
        "missing": list(dict.fromkeys(missing)),
        "unavailable": unavailable,
        "ready": bool(draft and draft.get("actions") and not unavailable),
    }
