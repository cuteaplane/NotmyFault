from __future__ import annotations

import os
import shutil
import time
import secrets
import hashlib
import json
import tempfile
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class PluginFileSystem:
    def replace(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)

    def copy_tree(self, source: Path, destination: Path) -> None:
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    def remove_tree(self, path: Path) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass

    def discard_tree(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def exists(self, path: Path) -> bool:
        return path.exists()

    def make_dirs(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)


class PluginTemporaryStorage:
    def write_archive(self, data: bytes) -> Path:
        temporary = tempfile.NamedTemporaryFile(delete=False, suffix=".nmfp")
        try:
            temporary.write(data)
        finally:
            temporary.close()
        return Path(temporary.name)

    def create_directory(self) -> Path:
        return Path(tempfile.mkdtemp())

    def remove_file(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class PluginInstallTransaction:
    def __init__(self, file_system: PluginFileSystem) -> None:
        self._file_system = file_system

    def install_tree(
        self,
        source: Path,
        destination: Path,
        validate: Callable[[Path], None] | None = None,
        keep_backup: bool = True,
        obsolete_destinations: tuple[Path, ...] = (),
    ) -> tuple[Path, ...]:
        self._file_system.make_dirs(destination.parent)
        staging = destination.parent / (
            f".{destination.name}-staging-{secrets.token_hex(8)}"
        )
        displaced: list[tuple[Path, Path]] = []
        self._file_system.remove_tree(staging)
        try:
            self._file_system.copy_tree(source, staging)
            if validate is not None:
                validate(staging)
            destinations = [*obsolete_destinations, destination]
            unique_destinations = list(dict.fromkeys(destinations))
            for current in unique_destinations:
                if not self._file_system.exists(current):
                    continue
                backup = current.with_name(current.name + ".nmf-backup")
                self._file_system.remove_tree(backup)
                self._file_system.replace(current, backup)
                displaced.append((current, backup))
            try:
                self._file_system.replace(staging, destination)
            except Exception:
                for current, backup in reversed(displaced):
                    self._file_system.replace(backup, current)
                displaced.clear()
                raise
        except Exception:
            for current, backup in reversed(displaced):
                if self._file_system.exists(backup):
                    self._file_system.replace(backup, current)
            displaced.clear()
            raise
        finally:
            self._file_system.discard_tree(staging)
        backups = tuple(backup for _current, backup in displaced)
        if not keep_backup:
            for backup in backups:
                self._file_system.discard_tree(backup)
            return ()
        return backups

    def restore_backup(
        self,
        backup: Path,
        destination: Path,
        active: Path | None,
    ) -> Path:
        if not self._file_system.exists(backup):
            raise OSError(f"插件备份不存在: {backup}")
        failed = None
        if active is not None and self._file_system.exists(active):
            failed = active.with_name(
                f".{active.name}-failed-{secrets.token_hex(8)}"
            )
            self._file_system.remove_tree(failed)
            self._file_system.replace(active, failed)
        try:
            if self._file_system.exists(destination):
                raise OSError(f"旧插件目录仍然存在: {destination}")
            self._file_system.replace(backup, destination)
        except Exception:
            if (
                failed is not None
                and self._file_system.exists(failed)
                and active is not None
                and not self._file_system.exists(active)
            ):
                self._file_system.replace(failed, active)
            raise
        if failed is not None:
            self._file_system.remove_tree(failed)
        return destination


@dataclass(frozen=True, slots=True)
class RetainedPluginBackup:
    kind: str
    manifest_name: str
    backup_path: Path
    destination: Path
    backup_meta: dict[str, Any]
    active_path: Path | None
    active_meta: dict[str, Any] | None
    problem: str | None = None


class PluginBackupStore:
    def __init__(
        self,
        user_plugins_dir: Path,
        file_system: PluginFileSystem,
    ) -> None:
        self._user_plugins_dir = user_plugins_dir
        self._transaction = PluginInstallTransaction(file_system)

    @staticmethod
    def _read_meta(path: Path, manifest_name: str) -> dict[str, Any] | None:
        try:
            value = json.loads(
                (path / manifest_name).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _package_name(meta: dict[str, Any] | None) -> str:
        if not meta:
            return ""
        value = meta.get("package_name") or meta.get("id")
        return value if isinstance(value, str) else ""

    def retained(self) -> tuple[RetainedPluginBackup, ...]:
        result: list[RetainedPluginBackup] = []
        for kind, folder_name, manifest_name in (
            ("trigger", "triggers", "trigger.json"),
            ("action", "actions", "action.json"),
        ):
            root = self._user_plugins_dir / folder_name
            try:
                children = tuple(root.iterdir())
            except OSError:
                continue
            active_dirs = tuple(
                path
                for path in children
                if path.is_dir()
                and not path.name.startswith(".")
                and not path.name.endswith(".nmf-backup")
            )
            for backup in children:
                if not backup.is_dir() or not backup.name.endswith(".nmf-backup"):
                    continue
                destination = backup.with_name(
                    backup.name[: -len(".nmf-backup")]
                )
                backup_meta = self._read_meta(backup, manifest_name)
                if backup_meta is None:
                    result.append(
                        RetainedPluginBackup(
                            kind,
                            manifest_name,
                            backup,
                            destination,
                            {},
                            None,
                            None,
                            "旧版插件清单无法读取",
                        )
                    )
                    continue
                package_name = self._package_name(backup_meta)
                candidates: list[tuple[Path, dict[str, Any] | None]] = []
                for active in active_dirs:
                    active_meta = self._read_meta(active, manifest_name)
                    if active == destination or (
                        package_name
                        and self._package_name(active_meta) == package_name
                    ):
                        candidates.append((active, active_meta))
                if len(candidates) > 1:
                    result.append(
                        RetainedPluginBackup(
                            kind,
                            manifest_name,
                            backup,
                            destination,
                            backup_meta,
                            None,
                            None,
                            "同一个包找到多个当前插件目录",
                        )
                    )
                    continue
                active_path, active_meta = (
                    candidates[0] if candidates else (None, None)
                )
                result.append(
                    RetainedPluginBackup(
                        kind,
                        manifest_name,
                        backup,
                        destination,
                        backup_meta,
                        active_path,
                        active_meta,
                    )
                )
        selected: list[RetainedPluginBackup] = []
        grouped: dict[tuple[str, str], list[RetainedPluginBackup]] = {}
        for backup in result:
            package_name = self._package_name(backup.backup_meta)
            if backup.problem or not package_name:
                selected.append(backup)
                continue
            grouped.setdefault((backup.kind, package_name), []).append(backup)
        for backups in grouped.values():
            if len(backups) == 1:
                selected.append(backups[0])
                continue
            exact = [
                backup
                for backup in backups
                if backup.active_path is not None
                and backup.active_path == backup.destination
            ]
            if len(exact) == 1:
                selected.append(exact[0])
                continue
            first = backups[0]
            selected.append(
                RetainedPluginBackup(
                    first.kind,
                    first.manifest_name,
                    first.backup_path,
                    first.destination,
                    first.backup_meta,
                    None,
                    None,
                    "同一个包留下多个旧版目录，无法确定该恢复哪一个",
                )
            )
        return tuple(selected)

    def restore(self, backup: RetainedPluginBackup) -> Path:
        if backup.problem:
            raise OSError(backup.problem)
        return self._transaction.restore_backup(
            backup.backup_path,
            backup.destination,
            backup.active_path,
        )


class PendingPreviewStore(MutableMapping[str, dict[str, Any]]):
    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        remove_tree: Callable[[Path], None] | None = None,
        ttl_seconds: float = 1800,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock
        self._remove_tree = remove_tree or (
            lambda path: shutil.rmtree(path, ignore_errors=True)
        )
        self._ttl_seconds = ttl_seconds
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))
        self._items: dict[str, dict[str, Any]] = {}

    def __getitem__(self, key: str) -> dict[str, Any]:
        return self._items[key]

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        self._items[key] = value

    def __delitem__(self, key: str) -> None:
        del self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def purge_expired(self) -> None:
        now = self._clock()
        expired = [
            token
            for token, preview in self._items.items()
            if now - float(preview.get("created_at", 0)) > self._ttl_seconds
        ]
        for token in expired:
            preview = self._items.pop(token)
            extract_dir = preview.get("extract_dir")
            if isinstance(extract_dir, str) and extract_dir:
                self._remove_tree(Path(extract_dir))

    def create(self, preview: dict[str, Any]) -> str:
        self.purge_expired()
        token = self._token_factory()
        item = dict(preview)
        item["created_at"] = self._clock()
        self._items[token] = item
        return token

    def discard(self, token: str) -> None:
        preview = self._items.pop(token, None)
        if preview is None:
            return
        extract_dir = preview.get("extract_dir")
        if isinstance(extract_dir, str) and extract_dir:
            self._remove_tree(Path(extract_dir))


def snapshot_plugin_directory(root_path: str) -> dict[str, str] | None:
    root = Path(root_path).resolve()
    snapshot: dict[str, str] = {}
    try:
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            is_junction = getattr(path, "is_junction", lambda: False)
            if path.is_symlink() or is_junction():
                return None
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None
    return snapshot
