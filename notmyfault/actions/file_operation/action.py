from notmyfault.plugin_api import action_values
"""文件操作：复制、移动、删除、压缩和解压
失败时抛异常，由引擎记录为失败
"""

import os
import shutil
import stat
import tarfile
import zipfile

# 解压前按声明的条目数和总大小设上限，避免一个压缩包把磁盘写满
_MAX_ENTRIES = 20000
_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024


def _safe_unpack(archive: str, target: str) -> None:
    """解压时检查成员路径和符号链接，非法成员抛 ValueError"""
    target_real = os.path.realpath(target)

    def check_member(name: str, is_link: bool) -> None:
        if is_link:
            raise ValueError(f"归档包含符号链接，拒绝解压: {name}")
        normalized = os.path.normpath(name)
        if normalized == ".." or normalized.startswith(".." + os.sep) or os.path.isabs(normalized):
            raise ValueError(f"归档包含非法路径: {name}")
        dest = os.path.join(target_real, normalized)
        dest_real = os.path.realpath(dest)
        if dest_real != target_real and not dest_real.startswith(os.path.join(target_real, "")):
            raise ValueError(f"归档成员越界: {name}")

    def check_limits(count: int, total: int) -> None:
        if count > _MAX_ENTRIES:
            raise ValueError(f"归档条目过多（{count} > {_MAX_ENTRIES}）")
        if total > _MAX_TOTAL_BYTES:
            raise ValueError(f"解压后大小超过上限（{total} 字节）")

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            check_limits(
                len(infos), sum(info.file_size for info in infos if not info.is_dir())
            )
            for info in infos:
                check_member(info.filename, False)
                name = info.filename.replace("/", os.sep)
                if os.path.altsep:
                    name = name.replace(os.path.altsep, os.sep)
                name = os.path.splitdrive(name)[1]
                parts = [part for part in name.split(os.sep) if part not in ("", os.curdir, os.pardir)]
                if os.name == "nt":
                    translation = str.maketrans(':<>|"?*', "_______")
                    parts = [part.translate(translation).rstrip(".") for part in parts]
                    parts = [part for part in parts if part]
                extracted_name = os.sep.join(parts)
                if not extracted_name and not info.is_dir():
                    raise ValueError(f"归档包含非法路径: {info.filename}")
                check_member(extracted_name, False)
            zf.extractall(target)
    else:
        with tarfile.open(archive) as tf:
            members = tf.getmembers()
            check_limits(
                len(members), sum(member.size for member in members if member.isfile())
            )
            for member in members:
                check_member(member.name, member.issym() or member.islnk())
                if not member.isfile() and not member.isdir():
                    raise ValueError(f"归档包含特殊文件，拒绝解压: {member.name}")
            tf.extractall(target, filter="data")


def _ensure_copy_safe(source: str, dest: str) -> None:
    """检查目标路径，目标在源目录内时抛 ValueError"""
    if not source or not dest:
        return
    src_real = os.path.realpath(source)
    dst_real = os.path.realpath(dest)
    if dst_real == src_real:
        raise ValueError(f"目标不能与源相同: {source}")
    if dst_real.startswith(os.path.join(src_real, "")):
        raise ValueError(f"目标位于源目录内部，会导致无限递归复制: {dest}")


def _ensure_compress_safe(source: str, dest: str) -> None:
    _ensure_copy_safe(source, dest)
    if not os.path.exists(dest):
        return

    def check_file(path):
        if os.path.samefile(path, dest):
            raise ValueError(f"压缩包与源文件指向同一文件: {path}")

    if not os.path.isdir(source):
        check_file(source)
        return

    def raise_walk_error(error):
        raise error

    for current, _, filenames in os.walk(source, followlinks=False, onerror=raise_walk_error):
        for name in filenames:
            path = os.path.join(current, name)
            if os.path.isfile(path):
                check_file(path)


def _move_to_target(source: str, dest: str, overwrite: bool) -> None:
    _ensure_copy_safe(source, dest)
    if not overwrite and os.path.lexists(dest):
        raise FileExistsError("目标已存在，请更换路径或允许覆盖")
    plain_directory = os.path.isdir(source) and not os.path.islink(source)
    if plain_directory and os.name == "nt":
        plain_directory = os.lstat(source).st_reparse_tag != stat.IO_REPARSE_TAG_MOUNT_POINT
    if plain_directory and os.path.isdir(dest):
        with os.scandir(source) as entries:
            for entry in entries:
                _move_to_target(entry.path, os.path.join(dest, entry.name), overwrite)
        os.rmdir(source)
    elif os.path.isdir(dest):
        raise IsADirectoryError(f"目标是目录，不能用文件或链接覆盖: {dest}")
    else:
        shutil.move(source, dest)


def run_with_context(action_info, params, context):
    params = action_values(params)
    operation = params.get("operation", "copy")
    source = params.get("source", "")
    dest = params.get("destination", "")
    overwrite = params.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ValueError("允许覆盖必须是布尔值")

    if not source:
        raise ValueError("未指定源路径")
    if not os.path.exists(source):
        raise FileNotFoundError(f"源路径不存在: {source}")
    if operation in ("copy", "move") and not dest:
        raise ValueError("未指定目标路径")
    if operation in ("copy", "move"):
        actual_dest = os.path.join(dest, os.path.basename(source.rstrip("/\\"))) if os.path.isdir(dest) and (operation == "move" or os.path.isfile(source)) else dest
        if not overwrite and os.path.lexists(actual_dest):
            raise FileExistsError("目标已存在，请更换路径或允许覆盖")

    print(f"[Action:file_operation] {operation}: {source} -> {dest}")

    if operation == "copy":
        _ensure_copy_safe(source, dest)
        if os.path.isdir(source):
            shutil.copytree(source, dest, dirs_exist_ok=overwrite)
        else:
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copy2(source, dest)
        print(f"[Action:file_operation] 复制完成")

    elif operation == "move":
        _ensure_copy_safe(source, actual_dest)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _move_to_target(source, actual_dest, overwrite)
        print(f"[Action:file_operation] 移动完成")

    elif operation == "delete":
        if os.path.isdir(source):
            shutil.rmtree(source)
        else:
            os.remove(source)
        print(f"[Action:file_operation] 删除完成")

    elif operation == "compress":
        if not dest:
            dest = source + ".zip"
        # make_archive 只会产出 base + ".zip"，目标名不带 .zip 时产物会换名字
        if not dest.lower().endswith(".zip"):
            dest += ".zip"
        base = os.path.splitext(dest)[0]
        dest = base + ".zip"
        _ensure_compress_safe(source, dest)
        if os.path.isdir(source):
            shutil.make_archive(base, "zip", root_dir=source)
        else:
            source_dir = os.path.dirname(source) or "."
            shutil.make_archive(
                base,
                "zip",
                root_dir=source_dir,
                base_dir=os.path.basename(source),
            )
        print(f"[Action:file_operation] 压缩完成: {dest}")

    elif operation == "extract":
        target = dest or source + "_extracted"
        os.makedirs(target, exist_ok=True)
        _safe_unpack(source, target)
        print(f"[Action:file_operation] 解压完成")

    else:
        raise ValueError(f"不支持的操作: {operation}")
