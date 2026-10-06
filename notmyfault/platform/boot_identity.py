import ctypes
import sys
from pathlib import Path


def boot_id() -> str:
    if sys.platform.startswith("linux"):
        return "linux:" + Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if sys.platform == "win32":
        class TimeOfDay(ctypes.Structure):
            _fields_ = [
                ("boot_time", ctypes.c_int64), ("current_time", ctypes.c_int64),
                ("time_zone_bias", ctypes.c_int64), ("time_zone_id", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32), ("boot_time_bias", ctypes.c_uint64),
                ("sleep_time_bias", ctypes.c_uint64),
            ]

        query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
        query.argtypes = [ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        query.restype = ctypes.c_int32
        value, returned = TimeOfDay(), ctypes.c_uint32()
        status = query(3, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned))
        if status < 0 or value.boot_time <= 0:
            raise OSError(f"无法读取系统启动时间：NTSTATUS 0x{status & 0xffffffff:08x}")
        return f"windows:{value.boot_time}"
    raise RuntimeError("当前系统不提供启动标识")
