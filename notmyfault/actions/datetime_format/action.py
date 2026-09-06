import math
from datetime import date, datetime, timedelta, timezone
from notmyfault.plugin_api import data_types_api


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    source = params.get("value", "")
    input_type = params.get("input_type", "iso")
    if input_type == "timestamp":
        source = data_types_api().convert_value(params.get("timestamp", 0), "datetime", source={"type": "timestamp", "unit": "seconds"}, options={"timezone": "utc"})
    elif input_type != "iso":
        raise ValueError("输入类型必须为 iso 或 timestamp")
    if isinstance(source, (date, datetime)):
        source = source.isoformat()
    if not isinstance(source, str):
        raise ValueError("日期时间必须是 ISO 8601 文本，留空表示当前时间")
    if source.strip():
        try:
            moment = datetime.fromisoformat(source.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("日期时间格式错误，请使用 ISO 8601，例如 2026-09-05T09:30:00+08:00") from None
        if moment.tzinfo is None:
            moment = moment.astimezone()
    else:
        moment = datetime.now().astimezone()
    zone = params.get("timezone", "local")
    if zone not in ("local", "utc", "preserve"):
        raise ValueError("时区必须为 local、utc 或 preserve")
    unit = params.get("offset_unit", "days")
    multipliers = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}
    if unit not in multipliers:
        raise ValueError("时间偏移单位必须为 seconds、minutes、hours 或 days")
    amount = params.get("offset", 0)
    if isinstance(amount, bool):
        raise ValueError("时间偏移必须是有限数字")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError("时间偏移必须是有限数字") from None
    if not math.isfinite(amount):
        raise ValueError("时间偏移必须是有限数字")
    try:
        moment = moment + timedelta(seconds=amount * multipliers[unit])
    except (OverflowError, ValueError):
        raise ValueError("时间偏移超出可表示的日期范围") from None
    if zone == "local":
        moment = moment.astimezone()
    elif zone == "utc":
        moment = moment.astimezone(timezone.utc)
    template = params.get("format", "%Y-%m-%d %H:%M:%S")
    if not isinstance(template, str):
        raise ValueError("日期格式必须是字符串")
    try:
        text = moment.strftime(template)
        timestamp = moment.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError("日期或格式超出当前系统支持的范围") from None
    return {
        "text": text,
        "iso": moment.isoformat(),
        "timestamp": timestamp,
        "date": moment.date().isoformat(),
        "time": moment.timetz().isoformat(),
    }
