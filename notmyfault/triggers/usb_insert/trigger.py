import time
import psutil
import os


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "usb_insert")
    print(f"[Trigger:{trigger_id}] U盘监视雷达已启动！")
    expected_drive = config.get("drive_letter", "").strip().upper()

    # 帮助函数：获取当前所有的可移动磁盘盘符 (比如 {'E:', 'F:'})
    def get_removable_drives():
        drives = set()
        for p in psutil.disk_partitions(all=False):
            if "removable" in p.opts or (
                os.name != "nt"
                and p.mountpoint.startswith(("/media/", "/run/media/"))
            ):
                # p.device 通常长这样: 'E:\\'，我们截取前两个字符 'E:'
                drives.add(
                    p.device[:2].upper() if os.name == "nt" else p.mountpoint
                )
        return drives

    # 1. 启动时先摸底，把已经插在电脑上的 U盘记录下来，防止刚开机就误报！
    last_drives = get_removable_drives()

    # 2. 开始持续监听
    while not shutdown_event.is_set():
        try:
            current_drives = get_removable_drives()
            
            # 集合减法：现在的 U盘 减去 刚才的 U盘 = 新插进来的 U盘！
            new_drives = current_drives - last_drives

            if new_drives:
                for drive in new_drives:
                    print(f"[Trigger:{trigger_id}] 捕捉到新U盘插入: {drive}")
                    
                    # 留空与 "ANY" 等价：匹配任意 U 盘插入。
                    if expected_drive in ("ANY", "") or expected_drive == drive.upper():
                        emit_event({"drive_letter": expected_drive, "actual_drive": drive})

            # 更新历史小本本
            last_drives = current_drives

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 哎呀，扫描U盘的时候报错啦: {e}")

        # 每3秒扫描一次就足够啦
        shutdown_event.wait(3)
