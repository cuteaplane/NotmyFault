"""压缩文件夹或文件为 ZIP，以及解压 ZIP
路径无效、不是 ZIP 或解压成员不合法时返回 ok=false，不抛给引擎当硬失败
"""

from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path
from typing import Any

# 解压前先按声明的条目数和总大小设上限，避免一个压缩包把磁盘写满
_MAX_ENTRIES = 20000
_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024


def _soft(error: str, **extra: Any) -> dict[str, Any]:
    result = {"ok": False, "error": error}
    result.update(extra)
    return result


def _require_nonempty(name: str, value) -> str:
    text = str(value or "")
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
    if dest_real != target_real and not dest_real.startswith(os.path.join(target_real, "")):
        raise ValueError(f"归档成员越界: {name}")


def _member_destination(name: str, target_real: str) -> str:
    _check_member(name, target_real)
    name = name.replace("/", os.sep)
    if os.path.altsep:
        name = name.replace(os.path.altsep, os.sep)
    name = os.path.splitdrive(name)[1]
    parts = [part for part in name.split(os.sep) if part not in ("", os.curdir, os.pardir)]
    if os.name == "nt":
        translation = str.maketrans(':<>|"?*', "_______")
        parts = [part.translate(translation).rstrip(".") for part in parts]
        parts = [part for part in parts if part]
    relative = os.sep.join(parts)
    _check_member(relative, target_real)
    return os.path.join(target_real, relative)


def _copy_stream(source, output, cancellation=None) -> None:
    while True:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        chunk = source.read(64 * 1024)
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if not chunk:
            return
        output.write(chunk)


def _zip_sources(sources: list[Path], archive: Path, cancellation=None) -> int:
    entries = []

    def raise_walk_error(error):
        raise error

    for source in sources:
        source = source.absolute()
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if source.is_dir():
            for current, directories, filenames in os.walk(source, followlinks=False, onerror=raise_walk_error):
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                directories.sort()
                if not directories and not filenames:
                    arcname = Path(current).relative_to(source.parent).as_posix()
                    if arcname != ".":
                        entries.append((None, arcname.rstrip("/") + "/"))
                for name in sorted(filenames):
                    if cancellation is not None:
                        cancellation.raise_if_cancelled()
                    path = Path(current) / name
                    if stat.S_ISREG(path.lstat().st_mode):
                        entries.append((path, path.relative_to(source.parent).as_posix()))
        elif source.is_file():
            entries.append((source, source.name))
        else:
            raise FileNotFoundError(f"源路径不存在: {source}")
    for path, _ in entries:
        if path is not None and archive.exists() and archive.samefile(path):
            raise ValueError(f"压缩包与源文件指向同一文件: {path}")
    count = 0
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in entries:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            if path is None:
                zf.writestr(arcname, "")
            else:
                info = zipfile.ZipInfo.from_file(path, arcname)
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(path, "rb") as source, zf.open(info, "w") as output:
                    _copy_stream(source, output, cancellation)
                count += 1
    return count


def _unzip(
    archive: Path,
    target: Path,
    cancellation=None,
    overwrite: bool = False,
) -> tuple[int, int]:
    target.mkdir(parents=True, exist_ok=True)
    target_real = os.path.realpath(target)
    written = 0
    skipped = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise ValueError(f"压缩包条目过多（{len(infos)} > {_MAX_ENTRIES}）")
        declared = sum(info.file_size for info in infos if not info.is_dir())
        if declared > _MAX_TOTAL_BYTES:
            raise ValueError(f"解压后大小超过上限（{declared} 字节）")
        destinations = {}
        for info in infos:
            destinations[id(info)] = _member_destination(info.filename, target_real)
        # 取消后已写出的内容保留，当前文件可能不完整
        for info in infos:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            destination = destinations[id(info)]
            if info.is_dir():
                os.makedirs(destination, exist_ok=True)
                continue
            if not overwrite and os.path.exists(destination):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            try:
                with zf.open(info) as source, open(destination, "wb" if overwrite else "xb") as output:
                    _copy_stream(source, output, cancellation)
            except FileExistsError:
                if overwrite:
                    raise
                skipped += 1
                continue
            written += 1
    return written, skipped


def run_with_context(action_info, params, context):
    cancellation = context.get("runtime", {}).get("cancellation")
    mode = str(params.get("mode", "zip") or "zip").strip().lower()
    if mode not in {"zip", "unzip"}:
        raise ValueError(f"未知模式: {mode!r}（可选: zip/unzip）")
    overwrite = params.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ValueError("允许覆盖必须是布尔值")

    if mode == "zip":
        source_raw = _require_nonempty("源路径", params.get("source", ""))
        source = _expand(source_raw)
        dest_raw = str(params.get("destination", "") or "")
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
            if archive.resolve().is_relative_to(source.resolve()):
                return _soft(
                    f"压缩包路径不能位于源目录内部或与源相同: {archive}",
                    mode=mode,
                    archive=str(archive),
                )
        except OSError as error:
            return _soft(f"无法解析路径: {error}", mode=mode, archive=str(archive))

        try:
            file_count = _zip_sources([source], archive, cancellation)
        except (OSError, FileNotFoundError, ValueError) as error:
            return _soft(str(error), mode=mode, archive=str(archive))

        print(f"[Action:archive] 已压缩: {archive}（{file_count} 个文件）")
        return {
            "ok": True,
            "mode": mode,
            "archive": str(archive.resolve()),
            "source": str(source.resolve()),
            "file_count": file_count,
            "skipped": 0,
            "error": "",
        }

    # unzip
    archive_raw = _require_nonempty("压缩包路径", params.get("source", ""))
    archive = _expand(archive_raw)
    dest_raw = str(params.get("destination", "") or "")
    target = _expand(dest_raw) if dest_raw else Path(str(archive) + "_extracted")

    if not archive.exists():
        return _soft(f"压缩包不存在: {archive}", mode=mode, archive=str(archive))
    if not archive.is_file():
        return _soft(f"压缩包路径不是文件: {archive}", mode=mode, archive=str(archive))
    if not zipfile.is_zipfile(archive):
        return _soft(f"不是有效的 ZIP 文件: {archive}", mode=mode, archive=str(archive))

    try:
        written, skipped = _unzip(archive, target, cancellation, overwrite)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        return _soft(str(error), mode=mode, archive=str(archive), destination=str(target))

    print(
        f"[Action:archive] 已解压: {archive} -> {target}"
        f"（{written} 个文件，跳过 {skipped} 个已存在文件）"
    )
    return {
        "ok": True,
        "mode": mode,
        "archive": str(archive.resolve()),
        "destination": str(target.resolve()),
        "file_count": written,
        "skipped": skipped,
        "error": "",
    }


def run(action_info, params):
    return run_with_context(action_info, params, {})
