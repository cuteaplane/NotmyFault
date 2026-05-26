import time
import psutil


def run(meta, config_list, emit_event):
    trigger_id = meta.get("id", "usb_insert")
    print(f"[Trigger:{trigger_id}] U盘监视雷达已启动！(๑•̀ㅂ•́)و✧")

    # 帮助函数：获取当前所有的可移动磁盘盘符 (比如 {'E:', 'F:'})
    def get_removable_drives():
        drives = set()
        for p in psutil.disk_partitions(all=False):
            if 'removable' in p.opts:
                # p.device 通常长这样: 'E:\\'，我们截取前两个字符 'E:'
                drives.add(p.device[:2].upper())
        return drives

    # 1. 启动时先摸底，把已经插在电脑上的 U盘记录下来，防止刚开机就误报！
    last_drives = get_removable_drives()

    # 2. 开始持续监听
    while True:
        try:
            current_drives = get_removable_drives()
            
            # 集合减法：现在的 U盘 减去 刚才的 U盘 = 新插进来的 U盘！
            new_drives = current_drives - last_drives

            if new_drives:
                for drive in new_drives:
                    print(f"[Trigger:{trigger_id}] 捕捉到新U盘插入: {drive}")
                    
                    # 遍历用户的规则进行模糊匹配
                    for config in config_list:
                        expected_drive = config.get("drive_letter", "").strip().upper()
                        
                        # 如果用户填了 "ANY" 或者精确匹配到了盘符 (比如 "E:")
                        if expected_drive == "ANY" or expected_drive == drive:
                            # 发射标准化事件给引擎！
                            emit_event(
                                trigger_id,
                                {
                                    "drive_letter": expected_drive
                                }
                            )

            # 更新历史小本本
            last_drives = current_drives

        except Exception as e:
            print(f"[Trigger:{trigger_id}] 哎呀，扫描U盘的时候报错啦: {e}")

        # 每3秒扫描一次就足够啦
        time.sleep(3)