"""NotmyFault 模拟环境 - 模拟 Windows 系统状态，用于在受控环境中测试触发器和脚本。"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


class _SimProcessRef:
    """模拟的进程引用，模拟 psutil 进程对象的 .info 接口。"""
    def __init__(self, name: str, pid: int):
        self.info = {"name": name, "pid": pid}


class _SimPartitionRef:
    """模拟的磁盘分区引用，模拟 psutil 分区的 .device/.opts 接口。"""
    def __init__(self, device: str, opts: str):
        self.device = device
        self.opts = opts


class SimProcessManager:
    """管理模拟进程集合。"""
    def __init__(self):
        self._next_pid = 1000
        self._processes: Dict[int, _SimProcessRef] = {}

    def add(self, name: str, pid: Optional[int] = None) -> int:
        pid = pid or self._next_pid
        self._next_pid = max(self._next_pid, pid) + 1
        self._processes[pid] = _SimProcessRef(name, pid)
        return pid

    def remove(self, name_or_pid):
        if isinstance(name_or_pid, int):
            self._processes.pop(name_or_pid, None)
        else:
            for pid in list(self._processes.keys()):
                if self._processes[pid].info["name"] == name_or_pid:
                    del self._processes[pid]

    def clear(self):
        self._processes.clear()

    def process_iter(self, attrs=None):
        """模拟 psutil.process_iter()。返回进程引用列表。"""
        return list(self._processes.values())


class SimUSBManager:
    """管理模拟的 USB 驱动器。"""
    def __init__(self):
        self._drives: Dict[str, str] = {}

    def insert(self, drive_letter: str, label: str = "") -> None:
        drive_letter = drive_letter[0].upper() + ":"
        self._drives[drive_letter] = label

    def remove(self, drive_letter: str) -> None:
        drive_letter = drive_letter[0].upper() + ":"
        self._drives.pop(drive_letter, None)

    def clear(self) -> None:
        self._drives.clear()

    def disk_partitions(self, all=False):
        """模拟 psutil.disk_partitions()。返回分区引用列表。"""
        result = []
        for drive in self._drives:
            result.append(_SimPartitionRef(device=drive + "\\", opts="removable"))
        return result


class SimWindowManager:
    """管理模拟的窗口。"""
    def __init__(self):
        self._windows: Dict[int, str] = {}

    def open(self, title: str) -> int:
        hwnd = 10000 + len(self._windows)
        self._windows[hwnd] = title
        return hwnd

    def close(self, title_or_hwnd):
        if isinstance(title_or_hwnd, int):
            self._windows.pop(title_or_hwnd, None)
        else:
            for hwnd in list(self._windows.keys()):
                if self._windows[hwnd] == title_or_hwnd:
                    del self._windows[hwnd]

    def clear(self):
        self._windows.clear()

    def enum_windows(self, callback, lparam):
        """模拟 user32.EnumWindows()。对每个窗口调用 callback。"""
        for hwnd in list(self._windows.keys()):
            callback(hwnd, lparam)

    def is_window_visible(self, hwnd):
        return hwnd in self._windows

    def get_window_text_length(self, hwnd):
        return len(self._windows.get(hwnd, ""))

    def get_window_text(self, hwnd, buf, size):
        title = self._windows.get(hwnd, "")
        truncated = title[:size-1]
        for i, ch in enumerate(truncated):
            buf[i] = ch
        return len(truncated)


class SimIdleManager:
    """管理模拟的系统空闲状态。"""
    def __init__(self):
        self._idle_seconds: float = 0.0
        self._tick_base: int = 0

    def set_idle(self, seconds: float) -> None:
        self._idle_seconds = seconds

    def get_tick_count(self) -> int:
        return self._tick_base + int(self._idle_seconds * 1000)

    def get_last_input_info(self, lii) -> bool:
        lii.dwTime = self._tick_base
        lii.cbSize = 8
        return True


class SimTimeManager:
    """管理模拟的时间。"""
    def __init__(self, start_time: Optional[datetime] = None):
        self._now: datetime = start_time or datetime(2026, 6, 1, 0, 0, 0)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def now(self) -> datetime:
        return self._now

    def set_now(self, dt: datetime) -> None:
        self._now = dt


class SimBluetoothManager:
    """管理模拟的蓝牙设备。"""
    def __init__(self):
        self._devices: Dict[str, bool] = {}

    def connect(self, device_name: str) -> None:
        self._devices[device_name] = True

    def disconnect(self, device_name: str) -> None:
        self._devices[device_name] = False

    def remove(self, device_name: str) -> None:
        self._devices.pop(device_name, None)

    def clear(self) -> None:
        self._devices.clear()

    def query_powershell(self, script: str) -> Tuple[str, str, int]:
        """模拟蓝牙的 PowerShell 查询。返回 (stdout, stderr, returncode)。"""
        lines = []
        for name, connected in self._devices.items():
            base_iid = "DEV_" + str(abs(hash(name)) % 100000).zfill(5)
            conn_str = "yes" if connected else "no"
            lines.append(f"{name}|{conn_str}|{base_iid}")
        stdout = "\n".join(lines)
        return (stdout, "", 0)


class SimulatedEnvironment:
    """模拟的 Windows 系统环境。

    组合所有子管理器，提供统一的模拟环境接口。
    用于在受控环境中测试 NotmyFault 引擎和触发器。
    """
    def __init__(self, start_time: Optional[datetime] = None):
        self.processes = SimProcessManager()
        self.usb = SimUSBManager()
        self.windows = SimWindowManager()
        self.idle = SimIdleManager()
        self.time = SimTimeManager(start_time)
        self.bluetooth = SimBluetoothManager()

    def reset(self) -> None:
        """重置所有模拟状态。"""
        self.processes.clear()
        self.usb.clear()
        self.windows.clear()
        self.idle.set_idle(0.0)
        self.time.set_now(datetime(2026, 6, 1, 0, 0, 0))
        self.bluetooth.clear()
