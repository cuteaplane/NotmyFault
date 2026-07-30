import os
import shutil


def run(action_info, params):
    operation = params.get("operation", "copy")
    source = params.get("source", "").strip()
    dest = params.get("destination", "").strip()

    if not source:
        print("[Action:file_operation] 未指定源路径")
        return

    if not os.path.exists(source):
        print(f"[Action:file_operation] 源路径不存在: {source}")
        return

    print(f"[Action:file_operation] {operation}: {source} -> {dest}")

    try:
        if operation == "copy":
            if os.path.isdir(source):
                shutil.copytree(source, dest, dirs_exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                shutil.copy2(source, dest)
            print(f"[Action:file_operation] 复制完成")

        elif operation == "move":
            shutil.move(source, dest)
            print(f"[Action:file_operation] 移动完成")

        elif operation == "delete":
            if os.path.isdir(source):
                shutil.rmtree(source, ignore_errors=True)
            else:
                os.remove(source)
            print(f"[Action:file_operation] 删除完成")

        elif operation == "compress":
            if not dest:
                dest = source + ".zip"
            shutil.make_archive(
                dest.replace(".zip", ""), "zip", source
            )
            print(f"[Action:file_operation] 压缩完成: {dest}.zip")

        elif operation == "extract":
            if not os.path.exists(source):
                print(f"[Action:file_operation] 压缩文件不存在: {source}")
                return
            os.makedirs(dest or source + "_extracted", exist_ok=True)
            shutil.unpack_archive(source, dest or source + "_extracted")
            print(f"[Action:file_operation] 解压完成")

    except Exception as e:
        print(f"[Action:file_operation] 操作失败: {e}")
