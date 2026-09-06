"""文件操作：复制、移动、删除、压缩和解压
失败时抛异常，由引擎记录为失败
"""

import os
import shutil
import tarfile
import zipfile


def _safe_unpack(archive: str, target: str) -> None:
    """解压时检查成员路径和符号链接，非法成员抛 ValueError"""
    target_real = os.path.realpath(target)

    def check_member(name: str, is_link: bool) -> None:
        if is_link:
            raise ValueError(f"归档包含符号链接，拒绝解压: {name}")
        normalized = os.path.normpath(name)
        if normalized.startswith("..") or os.path.isabs(normalized):
            raise ValueError(f"归档包含非法路径: {name}")
        dest = os.path.join(target_real, normalized)
        dest_real = os.path.realpath(dest)
        if dest_real != target_real and not dest_real.startswith(target_real + os.sep):
            raise ValueError(f"归档成员越界: {name}")

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                check_member(info.filename, False)
            zf.extractall(target)
    else:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                check_member(member.name, member.issym() or member.islnk())
            tf.extractall(target)


def _ensure_copy_safe(source: str, dest: str) -> None:
    """检查目标路径，目标在源目录内时抛 ValueError"""
    if not source or not dest:
        return
    src_real = os.path.realpath(source)
    dst_real = os.path.realpath(dest)
    if dst_real == src_real:
        raise ValueError(f"目标不能与源相同: {source}")
    if dst_real.startswith(src_real + os.sep):
        raise ValueError(f"目标位于源目录内部，会导致无限递归复制: {dest}")


def run(action_info, params):
    operation = params.get("operation", "copy")
    source = params.get("source", "").strip()
    dest = params.get("destination", "").strip()

    if not source:
        raise ValueError("未指定源路径")
    if not os.path.exists(source):
        raise FileNotFoundError(f"源路径不存在: {source}")

    print(f"[Action:file_operation] {operation}: {source} -> {dest}")

    if operation == "copy":
        _ensure_copy_safe(source, dest)
        if os.path.isdir(source):
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copy2(source, dest)
        print(f"[Action:file_operation] 复制完成")

    elif operation == "move":
        _ensure_copy_safe(source, dest)
        parent = os.path.dirname(dest)
        if parent and not os.path.isdir(source):
            os.makedirs(parent, exist_ok=True)
        shutil.move(source, dest)
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
        _ensure_copy_safe(source, dest)
        base = os.path.splitext(dest)[0]
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
