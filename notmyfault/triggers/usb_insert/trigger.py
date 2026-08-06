import time
import psutil
import os


def run(meta, config, emit_event, shutdown_event):
    trigger_id = meta.get("id", "usb_insert")
    print(f"[Trigger:{trigger_id}] U盘监视雷达已启动！")
    expected_drive = config.get("drive_letter", "").strip().upper()
    # 用户填 "e" 或 "E:" 都归一成 E:，和扫描出来的盘符格式对齐
    if expected_drive and expected_drive != "ANY":
        letter = expected_drive.rstrip(":")
        if len(letter) == 1 and letter.isalpha():
            expected_drive = letter + ":"

    def get_removable_drives():
        drives = set()
        for p in psutil.disk_partitions(all=False):
            if "removable" in p.opts or (
                os.name != "nt"
                and p.mountpoint.startswith(("/media/", "/run/media/"))
            ):
                # Windows p.device 通常以 E:\\ 开头，取前两个字符得到 E:
                drives.add(
                    p.device[:2].upper() if os.name == "nt" else p.mountpoint
                )
        return drives

    # 启动时记录已有可移动磁盘，首轮仅建立基线
    last_drives = get_removable_drives()

    while not shutdown_event.is_set():
        try:
            current_drives = get_removable_drives()
            
            # 集合差 current_drives - last_drives 表示新出现的磁盘
            new_drives = current_drives - last_drives

            if new_drives:
                for drive in new_drives:
                    print(f"[Trigger:{trigger_id}] 捕捉到新U盘插入: {drive}")
                    
                    # expected_drive 为空或为 ANY 时匹配所有新磁盘
                    if expected_drive in ("ANY", "") or expected_drive == drive.upper():
                        emit_event({"drive_letter": expected_drive, "actual_drive": drive})

            # 保存本轮磁盘集合供下一轮比较
            last_drives = current_drives

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 哎呀，扫描U盘的时候报错啦: {e}")

        shutdown_event.wait(3)
