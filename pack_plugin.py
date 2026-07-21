"""将插件源码目录打包为 .nmfp 安装包（7z 格式）。

用法:
  python pack_plugin.py <plugin_dir>
  python pack_plugin.py user_plugins/window_control
  python pack_plugin.py --all
"""
import json
import os
import sys
from pathlib import Path

import py7zr

ROOT = Path(__file__).parent.resolve()
USER_PLUGINS_DIR = ROOT / "user_plugins"
DIST_DIR = ROOT / "dist"


def _detect_json_name(plugin_dir: Path) -> str | None:
    """检测插件类型，返回主 json 文件名。"""
    for name in ("action.json", "trigger.json"):
        if (plugin_dir / name).exists():
            return name
    return None


_IGNORE_SUFFIXES = {".pyc"}
_IGNORE_NAMES = {"__pycache__", "signature.sig"}


def _collect_files(plugin_dir: Path) -> list[tuple[Path, str]]:
    """递归收集插件文件，排除 __pycache__/*.pyc/signature.sig。"""
    result = []
    for p in sorted(plugin_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix in _IGNORE_SUFFIXES:
            continue
        if any(part in _IGNORE_NAMES for part in p.parts):
            continue
        result.append((p, p.relative_to(plugin_dir.parent).as_posix()))
    return result


def pack_plugin(plugin_dir: Path, output_dir: Path = DIST_DIR) -> Path | None:
    """打包单个插件目录为 .nmfp，返回输出路径。"""
    plugin_dir = plugin_dir.resolve()
    if not plugin_dir.is_dir():
        print(f"! 目录不存在: {plugin_dir}", file=sys.stderr)
        return None

    json_name = _detect_json_name(plugin_dir)
    if not json_name:
        print(f"! 未找到 action.json 或 trigger.json: {plugin_dir}", file=sys.stderr)
        return None

    meta = json.loads((plugin_dir / json_name).read_text(encoding="utf-8"))
    plugin_id = meta.get("id", plugin_dir.name)
    ptype = "action" if json_name == "action.json" else "trigger"

    output_dir.mkdir(parents=True, exist_ok=True)
    nmfp_path = output_dir / f"{plugin_id}.nmfp"

    files = _collect_files(plugin_dir)
    with py7zr.SevenZipFile(nmfp_path, mode="w") as archive:
        for fpath, arcname in files:
            archive.write(fpath, arcname=arcname)

    print(f"+ {ptype:8s} {plugin_id:20s} -> {nmfp_path} ({len(files)} files)")
    return nmfp_path


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if args[0] == "--all":
        if not USER_PLUGINS_DIR.is_dir():
            print(f"! 用户插件目录不存在: {USER_PLUGINS_DIR}", file=sys.stderr)
            sys.exit(1)
        count = 0
        for p in sorted(USER_PLUGINS_DIR.iterdir()):
            if p.is_dir() and not p.name.startswith("_"):
                if pack_plugin(p):
                    count += 1
        print(f"已打包 {count} 个插件到 {DIST_DIR}")
        return

    plugin_dir = Path(args[0])
    if not plugin_dir.is_absolute():
        plugin_dir = ROOT / plugin_dir
    result = pack_plugin(plugin_dir)
    if result:
        print(f"完成: {result}")


if __name__ == "__main__":
    main()
