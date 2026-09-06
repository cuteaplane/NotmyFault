import psutil
import os

from notmyfault.triggers.base import PollingTrigger


class UsbInsertTrigger(PollingTrigger):
    interval = 3.0

    def validate(self):
        self.expected_drive = self.config.get("drive_letter", "").strip().upper()
        # 用户填 e 或 E: 都归一成 E:
        if self.expected_drive and self.expected_drive != "ANY":
            letter = self.expected_drive.rstrip(":")
            if len(letter) == 1 and letter.isalpha():
                self.expected_drive = letter + ":"

    @staticmethod
    def _get_removable_drives():
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

    def setup(self):
        self._last_drives = self._get_removable_drives()
        self.log("U盘监控已启动")

    def poll(self):
        current_drives = self._get_removable_drives()
        for drive in sorted(current_drives - self._last_drives):
            if self._stop_event.is_set():
                return
            self.log(f"检测到U盘插入: {drive}")
            if self.expected_drive in ("ANY", "") or self.expected_drive == drive.upper():
                self.emit({"drive_letter": self.expected_drive, "actual_drive": drive})
        self._last_drives = current_drives


def run(meta, config, emit_event, shutdown_event):
    UsbInsertTrigger(meta, config, emit_event, shutdown_event).run()
