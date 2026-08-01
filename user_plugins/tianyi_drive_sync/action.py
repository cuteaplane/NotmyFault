"""天翼云盘增量上传用户插件。

它只负责扫描、去重、上传并输出本次成功文件；Excel、通知等后续动作由工作流
组合。授权信息始终通过环境变量引用，绝不进入 NMF 的规则 JSON。
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.utils import formatdate
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from notmyfault.platform.platform_support import get_config_dir


_UPLOAD_URL = "https://upload.cloud.189.cn/uploadFile.action"
_REQUEST_URI = "/uploadFile.action"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STATE_LOCK = threading.Lock()

_STATE_ROOT = Path(get_config_dir()) / "plugin-data" / "tianyi_drive_sync"


class SyncError(RuntimeError):
    """可展示给工作流的配置或远端错误。"""


def _require_text(params: dict[str, Any], name: str, label: str) -> str:
    value = str(params.get(name, "")).strip()
    if not value:
        raise SyncError(f"请填写{label}")
    return value


def _secret_from_env(params: dict[str, Any], name: str, label: str) -> str:
    env_name = _require_text(params, name, label)
    if not _ENV_NAME.fullmatch(env_name):
        raise SyncError(f"{label}必须是合法的环境变量名")
    value = os.environ.get(env_name, "")
    if not value:
        raise SyncError(f"未读取到环境变量 {env_name}；请在启动 NotmyFault 前设置它")
    return value


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_fingerprint(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _state_path(source: Path, parent_id: str) -> Path:
    key = hashlib.sha256(f"{source.resolve()}\0{parent_id}".encode("utf-8")).hexdigest()[:24]
    return _STATE_ROOT / f"{key}.json"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("files"), dict) else {"files": {}}
    except FileNotFoundError:
        return {"files": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"无法读取上传状态 {path}: {exc}") from exc


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _signature(access_token: str, app_secret: str, date: str) -> str:
    text = f"AccessToken={access_token}&Operate=PUT&RequestURI={_REQUEST_URI}&Date={date}"
    digest = hmac.new(app_secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _upload_file(path: Path, parent_id: str, access_token: str, app_secret: str, md5: str) -> dict[str, str]:
    date = formatdate(usegmt=True)
    size = path.stat().st_size
    headers = {
        "AccessToken": access_token,
        "Date": date,
        "Signature": _signature(access_token, app_secret, date),
        "EDrive-ParentFolderId": parent_id,
        "EDrive-FileName": path.name,
        "EDrive-FileMD5": md5,
        "EDrive-FileLength": str(size),
        "Content-Length": str(size),
        "Content-Type": "application/octet-stream",
    }
    try:
        # urllib 的 HTTPConnection 支持带 read() 的请求体；保持流式，避免把 1GB
        # 文件整个读进内存。
        with path.open("rb") as body:
            request = Request(_UPLOAD_URL, data=body, headers=headers, method="PUT")
            with urlopen(request, timeout=120) as response:
                raw = response.read()
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        raise SyncError(f"HTTP {exc.code}：{detail}") from exc
    except URLError as exc:
        raise SyncError(f"网络错误：{exc.reason}") from exc
    except OSError as exc:
        raise SyncError(f"读取或上传失败：{exc}") from exc

    result: dict[str, str] = {}
    try:
        root = ElementTree.fromstring(raw)
        for child in root.iter():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag in {"id", "name", "md5", "size"} and child.text:
                result[tag] = child.text
    except ElementTree.ParseError:
        # 旧接口偶有非规范 XML；HTTP 2xx 仍作为远端接受成功处理。
        pass
    return result


def _candidates(source: Path, recursive: bool) -> list[Path]:
    iterator = source.rglob("*") if recursive else source.glob("*")
    files: list[Path] = []
    for candidate in iterator:
        try:
            if candidate.is_file():
                files.append(candidate)
        except OSError:
            continue
    return sorted(files, key=lambda value: str(value).lower())


def run_with_context(action_info: dict[str, Any], params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    source = Path(_require_text(params, "source_folder", "归档文件夹")).expanduser()
    if not source.is_dir():
        raise SyncError(f"归档文件夹不存在或无法访问：{source}")
    source = source.resolve()
    parent_id = _require_text(params, "remote_parent_folder_id", "天翼目标文件夹 ID")
    access_token = _secret_from_env(params, "access_token_env", "AccessToken 环境变量名")
    app_secret = _secret_from_env(params, "app_secret_env", "AppSecret 环境变量名")
    recursive = bool(params.get("recursive", False))
    try:
        size_limit = max(1, int(float(params.get("max_file_size_mb", 100)) * 1024 * 1024))
    except (TypeError, ValueError) as exc:
        raise SyncError("单文件上限必须是数字") from exc

    with _STATE_LOCK:
        state_path = _state_path(source, parent_id)
        state = _read_state(state_path)
        entries: dict[str, dict[str, Any]] = state["files"]
        uploaded_files: list[dict[str, Any]] = []
        failed_files: list[dict[str, str]] = []
        skipped = 0

        for file_path in _candidates(source, recursive):
            relative = str(file_path.relative_to(source)).replace("\\", "/")
            try:
                size, mtime_ns = _file_fingerprint(file_path)
                previous = entries.get(relative, {})
                if previous.get("size") == size and previous.get("mtime_ns") == mtime_ns:
                    skipped += 1
                    continue
                if size > size_limit:
                    raise SyncError(f"文件为 {size} 字节，超过一次性上传上限 {size_limit} 字节")
                md5 = _file_md5(file_path)
                if _file_fingerprint(file_path) != (size, mtime_ns):
                    raise SyncError("上传前检测到文件仍在变化")
                remote = _upload_file(file_path, parent_id, access_token, app_secret, md5)
                record = {
                    "local_path": str(file_path), "relative_path": relative,
                    "remote_name": remote.get("name", file_path.name), "remote_id": remote.get("id", ""),
                    "size": size, "md5": md5,
                    "uploaded_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                }
                entries[relative] = {"size": size, "mtime_ns": mtime_ns, **record}
                _write_state(state_path, state)  # 重试不会把已成功的文件再传一次。
                uploaded_files.append(record)
            except (OSError, SyncError) as exc:
                failed_files.append({"local_path": str(file_path), "reason": str(exc)})

        return {"uploaded_files": uploaded_files, "skipped": skipped, "failed_files": failed_files}


def run(action_info: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    return run_with_context(action_info, params, {"event": {}, "steps": {}})
