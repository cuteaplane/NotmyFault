"""安全模式探测：只读环境变量与 build.json，不依赖 git。

以前 engine.py 会在运行时跑 `git rev-parse` 探测分支，但打包/CI 下根本
没有 .git 目录，探测结果不可靠。现在统一以 NOTMYFAULT_MODE 环境变量
与 build.json 为权威来源，git 那套全删了。
"""
import json as _json
import os
import sys
from enum import Enum
from typing import List, Tuple


class SecurityMode(Enum):
    STRICT = "strict"
    NORMAL = "normal"
    PERMISSIVE = "permissive"


def detect_security_mode() -> SecurityMode:
    """按 环境变量 > build.json > 默认 STRICT 的优先级决定安全模式。

    fresh source clone 没有 build.json（build.py 生成、被 .gitignore 忽略），
    默认 STRICT：内置插件因缺少 signature.sig 被拒载，引擎退出并提示先跑
    `python build.py`（生成签名 + build.json）。打包构建一定带 build.json，
    由 build.py 决定 strict/normal/permissive。篡改防护不在源码层硬编码，
    而是由打包者通过 build.py 选择。"""
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
        os.path.join(os.path.dirname(__file__), "..", "build.json"),
        os.path.join(os.getcwd(), "build.json"),
    ]
    for _bp in paths:
        try:
            # build.json 决定安全模式，必须先验签：未签名或签名无效（security_mode
            # 可能被改过）一律不信任，跳过它回退到 STRICT 兜底。
            from notmyfault.signing import verify_file
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

    # 没有任何显式配置：默认严格。fresh clone 必须先跑 build.py 生成签名与
    # build.json，否则内置插件因无签名被拒载。
    return SecurityMode.STRICT


def verify_core_integrity() -> Tuple[bool, List[str]]:
    """校验 notmyfault/*.py 核心源码是否与签名清单一致（防篡改 engine.py/config.py 等）。

    build.py 生成 integrity.json（{files: {name: sha256}}）并用 Ed25519 签名。
    这里先验清单签名，再重新哈希所有核心文件比对。清单缺失/签名无效/哈希不匹配
    都算不通过。

    局限：本函数本身在 security.py 里，security.py 也在清单中--若有人连本函数
    一起改掉以绕过校验，这里发现不了。这是自校验的固有弱点（验者不能验自身）；
    但单文件篡改（只改 engine.py）能被检出，配合插件签名与 build.json 签名构成
    纵深防御。
    """
    import hashlib
    pkg_dir = os.path.dirname(__file__)
    manifest = os.path.join(pkg_dir, "integrity.json")
    if not os.path.exists(manifest):
        return False, ["integrity.json (missing; run build.py)"]
    try:
        from notmyfault.signing import verify_file
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
