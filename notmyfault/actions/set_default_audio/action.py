from notmyfault.plugin_api import action_values, platform

_FLOW_LABEL = {"render": "播放", "capture": "录音"}


def run_with_context(action_info, params, context):
    params = action_values(params)
    device_type = str(params.get("device_type", "render") or "render").strip().lower()
    if device_type not in _FLOW_LABEL:
        raise ValueError(f"无效的设备类型: {device_type!r}（可选: render/capture）")
    target = str(params.get("device", "") or "").strip()
    if not target:
        raise ValueError("必须填写目标设备 id 或名称")
    print(f"[Action:set_default_audio] 设置默认{_FLOW_LABEL[device_type]}设备: {target}")
    result = {"device_type": device_type, **platform.audio.set_default(device_type, target)}
    print(f"[Action:set_default_audio] 完成: {result['device_id']!r} changed={result['changed']}")
    return result
