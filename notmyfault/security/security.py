"""读取 NOTMYFAULT_MODE 和签名 build.json，选择引擎安全模式。"""
import json as _json
import os
import sys
from enum import Enum
from pathlib import Path
from typing import List, Tuple


_PKG_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def is_admin_process() -> bool:
    """检查 Windows 提权令牌或 POSIX 有效 UID，探测异常时返回 False。"""

    if os.name != "nt":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        import ctypes
        from ctypes import wintypes

        class _TOKEN_ELEVATION(ctypes.Structure):
            _fields_ = [("TokenIsElevated", wintypes.DWORD)]

        TOKEN_QUERY = 0x0008
        TokenElevation = 20
        handle = wintypes.HANDLE()
        if not ctypes.windll.advapi32.OpenProcessToken(
            ctypes.windll.kernel32.GetCurrentProcess(),
            TOKEN_QUERY,
            ctypes.byref(handle),
        ):
            return False
        try:
            elevation = _TOKEN_ELEVATION()
            ok = ctypes.windll.advapi32.GetTokenInformation(
                handle,
                TokenElevation,
                ctypes.byref(elevation),
                ctypes.sizeof(elevation),
                ctypes.byref(wintypes.DWORD()),
            )
            return bool(ok and elevation.TokenIsElevated)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


class SecurityMode(Enum):
    STRICT = "strict"
    NORMAL = "normal"
    PERMISSIVE = "permissive"


def detect_security_mode() -> SecurityMode:
    """按环境变量、签名 build.json、默认 STRICT 的顺序选择安全模式。"""
    env_mode = os.environ.get("NOTMYFAULT_MODE", "").lower().strip()
    if env_mode == "alpha":
        return SecurityMode.PERMISSIVE
    if env_mode in ("develop", "dev"):
        return SecurityMode.NORMAL
    if env_mode in ("stable", "master"):
        return SecurityMode.STRICT

    paths = []
    if getattr(sys, "frozen", False):
        paths.append(os.path.join(sys._MEIPASS, "build.json"))
    paths += [
        str(_PROJECT_ROOT / "build.json"),
        os.path.join(os.getcwd(), "build.json"),
    ]
    for _bp in paths:
        try:
            # build.json 未通过签名校验时不读取其中的 security_mode。
            from notmyfault.security.signing import verify_file
            if not verify_file(_bp):
                continue
            with open(_bp, encoding="utf-8") as _bf:
                _bj = _json.load(_bf)
            _m = _bj.get("security_mode", "").lower().strip()
            if _m == "permissive":
                return SecurityMode.PERMISSIVE
            if _m == "normal":
                return SecurityMode.NORMAL
            if _m == "strict":
                return SecurityMode.STRICT
        except Exception:
            continue

    # 没有有效配置时使用 STRICT。
    return SecurityMode.STRICT


def verify_core_integrity() -> Tuple[bool, List[str]]:
    """校验核心源码是否匹配签名的 integrity.json 清单，清单缺失、签名无效、哈希不匹配或 security.py 被修改都会失败。"""
    import hashlib
    pkg_dir = str(_PKG_ROOT)
    manifest = os.path.join(pkg_dir, "integrity.json")
    if not os.path.exists(manifest):
        return False, ["integrity.json (missing; run build.py)"]
    try:
        from notmyfault.security.signing import verify_file
        if not verify_file(manifest):
            return False, ["integrity.json (signature invalid)"]
        with open(manifest, encoding="utf-8") as f:
            listed = _json.load(f).get("files", {})
    except Exception as e:
        return False, [f"integrity.json ({e})"]

    bad: List[str] = []
    for name, expected in listed.items():
        fp = os.path.join(pkg_dir, name)
        try:
            actual = hashlib.sha256(open(fp, "rb").read()).hexdigest()
        except OSError:
            bad.append(f"{name} (missing)")
            continue
        if actual != expected:
            bad.append(name)
    return (len(bad) == 0), bad
