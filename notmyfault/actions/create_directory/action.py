from pathlib import Path


def run(action_info, params):
    return run_with_context(action_info, params, {})


def run_with_context(action_info, params, context):
    directory = str(params.get("directory", "") or "").strip()
    if not directory:
        raise ValueError("未指定目录路径")
    path = Path(directory).expanduser().absolute()
    created = True
    try:
        path.mkdir(parents=True)
    except FileExistsError:
        if not path.is_dir():
            raise
        created = False
    return {"path": str(path), "created": created}
