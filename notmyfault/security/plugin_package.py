from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PluginPackageLimits:
    max_entries: int = 2000
    max_uncompressed_bytes: int = 500 * 1024 * 1024
    max_upload_bytes: int = 64 * 1024 * 1024


def extract_nmfp(
    archive_path: str,
    extract_dir: str,
    password: str | None,
    limits: PluginPackageLimits,
) -> None:
    import py7zr

    with py7zr.SevenZipFile(
        archive_path,
        mode="r",
        password=password or None,
    ) as archive:
        listed_members = archive.list()
        raw_members = list(getattr(archive, "files", ()))
        for member in raw_members:
            if any(
                bool(getattr(member, attribute, False))
                for attribute in ("is_symlink", "is_junction", "is_socket")
            ):
                name = str(getattr(member, "filename", ""))
                raise ValueError(f"插件包包含链接或特殊文件，拒绝安装: {name}")

        entries = 0
        total_uncompressed = 0
        for info in listed_members:
            entries += 1
            if entries > limits.max_entries:
                raise ValueError(
                    f"插件包条目过多（>{limits.max_entries}），疑似解压炸弹"
                )
            total_uncompressed += int(getattr(info, "uncompressed", 0) or 0)
            if total_uncompressed > limits.max_uncompressed_bytes:
                raise ValueError("插件包解压后体积过大，疑似解压炸弹")
            name = str(getattr(info, "filename", ""))
            normalized = os.path.normpath(name)
            if (
                normalized.startswith("..")
                or os.path.isabs(normalized)
                or re.match(r"^[a-zA-Z]:", normalized)
            ):
                raise ValueError(f"插件包包含非法路径: {name}")
            if any(
                bool(getattr(info, attribute, False))
                for attribute in ("is_symlink", "is_junction", "is_socket")
            ):
                raise ValueError(f"插件包包含链接或特殊文件，拒绝安装: {name}")
        archive.extractall(extract_dir)

    root = Path(extract_dir).resolve()
    for base, directories, file_names in os.walk(extract_dir):
        for name in [*directories, *file_names]:
            full_path = Path(base) / name
            is_junction = getattr(full_path, "is_junction", lambda: False)
            if full_path.is_symlink() or is_junction():
                raise ValueError(f"插件包包含链接，拒绝安装: {name}")
            try:
                full_path.resolve().relative_to(root)
            except (OSError, ValueError) as error:
                raise ValueError(f"插件包文件路径越界: {name}") from error
