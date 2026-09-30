"""压缩文件夹或文件为 ZIP，以及解压 ZIP
路径无效、不是 ZIP 或解压成员不合法时返回 ok=false，不抛给引擎当硬失败
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Any


def _soft(error: str, **extra: Any) -> dict[str, Any]:
    result = {"ok": False, "error": error}
    result.update(extra)
    return result


def _require_nonempty(name: str, value) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"未指定{name}")
    return text


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def _check_member(name: str, target_real: str) -> None:
    normalized = os.path.normpath(name)
    if normalized == ".." or normalized.startswith(".." + os.sep) or os.path.isabs(normalized):
        raise ValueError(f"归档包含非法路径: {name}")
    dest = os.path.join(target_real, normalized)
    dest_real = os.path.realpath(dest)
    if dest_real != target_real and not dest_real.startswith(target_real + os.sep):
        raise ValueError(f"归档成员越界: {name}")


def _zip_sources(sources: list[Path], archive: Path) -> int:
    count = 0
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for source in sources:
            if source.is_dir():
                for path in sorted(source.rglob("*")):
                    if path.is_symlink():
                        continue
                    if path.is_file():
                        arcname = path.relative_to(source.parent).as_posix()
                        zf.write(path, arcname)
                        count += 1
                    elif path.is_dir() and not any(path.iterdir()):
                        arcname = path.relative_to(source.parent).as_posix().rstrip("/") + "/"
                        zf.writestr(arcname, "")
            elif source.is_file():
                zf.write(source, source.name)
                count += 1
            else:
                raise FileNotFoundError(f"源路径不存在: {source}")
    return count


def _unzip(archive: Path, target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)
    target_real = os.path.realpath(target)
    count = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            _check_member(info.filename, target_real)
            # 跳过目录项本身的写出计数，只计文件
            if not info.is_dir():
                count += 1
        zf.extractall(target)
    return count


def run(action_info, params):
    mode = str(params.get("mode", "zip") or "zip").strip().lower()
    if mode not in {"zip", "unzip"}:
        raise ValueError(f"未知模式: {mode!r}（可选: zip/unzip）")

    if mode == "zip":
        source_raw = _require_nonempty("源路径", params.get("source", ""))
        source = _expand(source_raw)
        dest_raw = str(params.get("destination", "") or "").strip()
        if dest_raw:
            archive = _expand(dest_raw)
        else:
            archive = Path(str(source) + ".zip")
        if not str(archive).lower().endswith(".zip"):
            archive = Path(str(archive) + ".zip")

        if not source.exists():
            return _soft(f"源路径不存在: {source}", mode=mode, archive=str(archive))
        if not source.is_file() and not source.is_dir():
            return _soft(f"源路径不是文件或文件夹: {source}", mode=mode, archive=str(archive))
        try:
            if archive.resolve() == source.resolve() or str(archive.resolve()).startswith(
                str(source.resolve()) + os.sep
            ):
                return _soft(
                    f"压缩包路径不能位于源目录内部或与源相同: {archive}",
                    mode=mode,
                    archive=str(archive),
                )
        except OSError as error:
            return _soft(f"无法解析路径: {error}", mode=mode, archive=str(archive))

        try:
            file_count = _zip_sources([source], archive)
        except (OSError, FileNotFoundError, ValueError) as error:
            return _soft(str(error), mode=mode, archive=str(archive))

        print(f"[Action:archive] 已压缩: {archive}（{file_count} 个文件）")
        return {
            "ok": True,
            "mode": mode,
            "archive": str(archive.resolve()),
            "source": str(source.resolve()),
            "file_count": file_count,
            "error": "",
        }

    # unzip
    archive_raw = _require_nonempty("压缩包路径", params.get("source", ""))
    archive = _expand(archive_raw)
    dest_raw = str(params.get("destination", "") or "").strip()
    target = _expand(dest_raw) if dest_raw else Path(str(archive) + "_extracted")

    if not archive.exists():
        return _soft(f"压缩包不存在: {archive}", mode=mode, archive=str(archive))
    if not archive.is_file():
        return _soft(f"压缩包路径不是文件: {archive}", mode=mode, archive=str(archive))
    if not zipfile.is_zipfile(archive):
        return _soft(f"不是有效的 ZIP 文件: {archive}", mode=mode, archive=str(archive))

    try:
        file_count = _unzip(archive, target)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        return _soft(str(error), mode=mode, archive=str(archive), destination=str(target))

    print(f"[Action:archive] 已解压: {archive} -> {target}（{file_count} 个文件）")
    return {
        "ok": True,
        "mode": mode,
        "archive": str(archive.resolve()),
        "destination": str(target.resolve()),
        "file_count": file_count,
        "error": "",
    }
