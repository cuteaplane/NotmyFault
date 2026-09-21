import csv
import io
import json
import math


def _load_json(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式错误，第 {exc.lineno} 行第 {exc.colno} 列") from None


def _columns(value):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("列名必须是字符串数组")
    if any(not item for item in value) or len(set(value)) != len(value):
        raise ValueError("CSV 列名不能为空或重复")
    return value


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("CSV 数字必须是有限值")
        return str(value)
    raise ValueError("CSV 单元格只能包含文本、数字、布尔值或 null")


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    operation = params.get("operation", "parse")
    delimiter = params.get("delimiter", ",")
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in '\r\n"\x00':
        raise ValueError("CSV 分隔符必须是一个字符，且不能是换行、双引号或空字符")
    has_header = bool(params.get("has_header", True))
    value = params.get("value", "")
    if operation == "parse":
        if not isinstance(value, str):
            raise ValueError("解析 CSV 时输入必须是文本")
        try:
            rows = list(csv.reader(io.StringIO(value.lstrip("\ufeff"), newline=""), delimiter=delimiter, strict=True))
        except csv.Error as exc:
            raise ValueError(f"CSV 格式错误: {exc}") from None
        columns = _columns(rows.pop(0)) if has_header and rows else []
        width = len(columns) if has_header else (len(rows[0]) if rows else 0)
        if any(len(row) != width for row in rows):
            raise ValueError("CSV 每行的列数必须一致")
        records = [dict(zip(columns, row)) for row in rows] if has_header else rows
        return {"records": records, "columns": columns, "text": value, "row_count": len(records)}
    if operation != "stringify":
        raise ValueError("未知的 CSV 操作")
    records = _load_json(value)
    if not isinstance(records, list):
        raise ValueError("生成 CSV 时输入必须是记录数组或其 JSON 文本")
    raw_columns = params.get("columns", "")
    columns = [] if raw_columns == "" else _columns(_load_json(raw_columns))
    if not has_header and columns:
        raise ValueError("未启用表头时不能指定列名")
    rows = []
    if records and isinstance(records[0], dict):
        if not has_header:
            raise ValueError("对象记录需要启用表头")
        if not columns:
            columns = list(dict.fromkeys(key for record in records if isinstance(record, dict) for key in record))
            _columns(columns)
        if not columns:
            raise ValueError("CSV 对象记录至少需要一列")
        for record in records:
            if not isinstance(record, dict) or any(key not in columns for key in record):
                raise ValueError("CSV 对象记录必须使用声明的列名")
            rows.append([_cell(record.get(key)) for key in columns])
    else:
        width = len(columns) if columns else (len(records[0]) if records and isinstance(records[0], list) else 0)
        if has_header and records and not columns:
            raise ValueError("数组记录启用表头时须指定列名")
        for record in records:
            if not isinstance(record, list) or len(record) != width:
                raise ValueError("CSV 数组记录必须保持相同的列数")
            rows.append([_cell(item) for item in record])
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter, lineterminator="\r\n")
    if has_header and columns:
        writer.writerow(columns)
    writer.writerows(rows)
    return {"records": records, "columns": columns, "text": stream.getvalue(), "row_count": len(records)}
