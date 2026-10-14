from notmyfault.plugin_api import action_values
from pathlib import Path




def run_with_context(action_info, params, context):
    params = action_values(params)
    directory = str(params.get("directory", "") or "")
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
