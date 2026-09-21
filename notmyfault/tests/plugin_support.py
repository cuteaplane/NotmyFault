import json
import importlib.util
from pathlib import Path

import py7zr


def make_meta(plugin_kind: str, **overrides):
    meta = {
        "id": f"demo_{plugin_kind}",
        "name": "演示插件",
        "description": "演示用插件",
        "enabled": True,
        "version_code": 1,
        "version": "1.0",
        "package_name": f"com.test.demo_{plugin_kind}",
    }
    meta.update(overrides)
    return meta


def build_nmfp(tmp_path, meta, plugin_kind, tag="pkg", extra_files=None):
    json_name = "trigger.json" if plugin_kind == "triggers" else "action.json"
    source = tmp_path / f"source-{tag}" / f"plugin-{tag}"
    source.mkdir(parents=True)
    (source / json_name).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    entry = "trigger.py" if plugin_kind == "triggers" else "action.py"
    (source / entry).write_text(
        "def run(meta, params):\n    return {'ok': True}\n", encoding="utf-8"
    )
    for name, content in (extra_files or {}).items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    archive = tmp_path / f"plugin-{tag}.nmfp"
    with py7zr.SevenZipFile(archive, "w") as output:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                output.write(path, f"plugin-{tag}/{path.relative_to(source).as_posix()}")
    return archive


def post_archive(env, route, archive, data=None):
    with open(archive, "rb") as file:
        return env.client.post(
            route,
            headers=env.headers,
            data=data or {},
            files={"file": (archive.name, file, "application/octet-stream")},
        )


def load_plugin(ptype, name):
    filename = "action.py" if ptype == "actions" else "trigger.py"
    path = Path(__file__).resolve().parents[1] / ptype / name / filename
    spec = importlib.util.spec_from_file_location(f"scenario_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
