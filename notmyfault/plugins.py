"""插件安全检查工具集。

以前 plugins/ 包里把安全检查剁成了 4 个文件（integrity / signing /
sudo_check / security_scan），每个就几十行，互相 import 来 import 去
跟走迷宫似的。现在合成一个文件，按功能分区，谁也别再找不到谁。

这些函数只在 PluginLoader 的加载流水线里调用，在插件 exec 之前
做安检：AST 扫描看有没有偷渡危险 import，签名校验防篡改，
hash 清单查文件有没有被动过手脚。
"""
import ast
import hashlib
import json
import os
from typing import List, Optional, Set, Tuple

from notmyfault.config import CONFIG_FILE


# ============================================================================
# AST: sudo 导入检测
# ============================================================================

def check_sudo_import(py_file_path: str) -> bool:
    """扫描 .py 源码是否 import 了 notmyfault.sudo（AST 级别检查）。

    在插件 exec 之前跑这道安检：如果插件偷偷 import 了 sudo 但没在
    元数据里声明 admin 权限，引擎会知道该盯紧它。
    """
    try:
        with open(py_file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "notmyfault.sudo":
                        return True
            elif isinstance(node, ast.ImportFrom):
                if node.module == "notmyfault.sudo":
                    return True
                if node.module == "notmyfault":
                    for alias in node.names:
                        if alias.name == "sudo":
                            return True
        return False
    except Exception:
        return False


# ============================================================================
# AST: 危险能力扫描
# ============================================================================

_NATIVE_MODULES = {"ctypes", "win32api", "win32con", "pywintypes", "_winapi"}
_EXTERNAL_MODULES = {"subprocess"}
_OS_DANGEROUS = {
    "system", "popen", "startfile",
    "execv", "execve", "execl", "execlp", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
}
_SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
_CTYPES_DANGEROUS = {"windll", "CDLL", "WinDLL", "OleDLL", "WINFUNCTYPE", "CFUNCTYPE", "Structure"}
# 自提权信号：ShellExecute 系列（UAC runas 提权 API）+ "runas" 动词。
# 插件必须走 notmyfault.sudo.run_as_admin（声明 admin 权限），禁止自己直接提权。
_ELEVATION_FUNCS = {"shellexecute", "shellexecutew", "shellexecuteex", "shellexecutea"}
_ELEVATION_VERB = "runas"


def scan_plugin_capabilities(py_file_path: str) -> Set[str]:
    """返回插件源码使用的危险能力集合（未声明则加载时告警/拒绝）。

    检测两类绕过行为（需在清单 permissions 中声明）：
    - native_api: 直接调用原生系统 API（ctypes/windll/win32api 等），
      可绕过 sudo.run_as_admin 自行提权或操作系统底层。
    - external_binary: 执行外部二进制/进程（subprocess、os.system、os.popen、
      os.startfile、os.exec*/spawn*、shell=True）。

    admin 能力（import notmyfault.sudo）由上面的 check_sudo_import 负责，
    此处不重复。扫描在插件 exec 之前进行，避免执行未声明的危险代码。

    另外 self_elevation（自行提权）是一律禁止的能力，不可声明：
    - 检测 ShellExecute/ShellExecuteW/ShellExecuteEx 等调用，或任意字符串里出现
      "runas" 动词（UAC 提权）。插件要提权必须走 notmyfault.sudo.run_as_admin
      并在元数据声明 "admin"，禁止自己 ShellExecute("runas")/os.startfile(x,"runas")
      /PowerShell Start-Process -Verb RunAs 之类。

    注意：这是 best-effort 的静态 AST 扫描，不是安全边界。动态构造模块名、
    exec/eval 字符串、通过第三方包间接触发系统调用等仍可绕过。对 builtin 插件，
    Ed25519 签名才是真正的完整性边界；对 user/third_party 插件，这里的扫描只是
    第一道闸，STRICT/NORMAL 模式据此拒载明显未声明的危险能力。
    """
    caps: Set[str] = set()
    try:
        with open(py_file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception:
        return caps

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _NATIVE_MODULES:
                    caps.add("native_api")
                if top in _EXTERNAL_MODULES:
                    caps.add("external_binary")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0] if mod else ""
            if top in _NATIVE_MODULES:
                caps.add("native_api")
            if top in _EXTERNAL_MODULES:
                caps.add("external_binary")
            if top == "os":
                for alias in node.names:
                    if alias.name in _OS_DANGEROUS:
                        caps.add("external_binary")
        elif isinstance(node, ast.Attribute):
            base, attr = node.value, node.attr
            # ShellExecute/ShellExecuteW/ShellExecuteEx 等 UAC 提权 API（无论挂在哪个对象上）。
            if attr.lower() in _ELEVATION_FUNCS:
                caps.add("self_elevation")
            if isinstance(base, ast.Name):
                if base.id == "os" and attr in _OS_DANGEROUS:
                    caps.add("external_binary")
                if base.id == "subprocess" and attr in _SUBPROCESS_CALLS:
                    caps.add("external_binary")
                if base.id == "ctypes" and attr in _CTYPES_DANGEROUS:
                    caps.add("native_api")
        elif isinstance(node, ast.Call):
            func = node.func
            # 直接 ShellExecuteW(...)（from xx import ShellExecuteW 之后）。
            if isinstance(func, ast.Name) and func.id.lower() in _ELEVATION_FUNCS:
                caps.add("self_elevation")
            # __import__("subprocess") / importlib.import_module("ctypes") 动态导入。
            # 只能识别常量模块名；动态拼名字符串仍可绕过（见 docstring）。
            mod_arg = None
            if isinstance(func, ast.Name) and func.id == "__import__":
                mod_arg = node.args[0] if node.args else None
            elif (isinstance(func, ast.Attribute) and func.attr == "import_module"
                  and isinstance(func.value, ast.Name) and func.value.id == "importlib"):
                mod_arg = node.args[0] if node.args else None
            if isinstance(mod_arg, ast.Constant) and isinstance(mod_arg.value, str):
                top = mod_arg.value.split(".")[0]
                if top in _NATIVE_MODULES:
                    caps.add("native_api")
                if top in _EXTERNAL_MODULES:
                    caps.add("external_binary")
            # getattr(os, "system") / getattr(subprocess, "Popen") 动态属性取用。
            if (isinstance(func, ast.Name) and func.id == "getattr"
                    and len(node.args) >= 2):
                target, name_arg = node.args[0], node.args[1]
                if isinstance(target, ast.Name) and isinstance(name_arg, ast.Constant) \
                        and isinstance(name_arg.value, str):
                    attr = name_arg.value
                    if target.id == "os" and attr in _OS_DANGEROUS:
                        caps.add("external_binary")
                    elif target.id == "subprocess" and attr in _SUBPROCESS_CALLS:
                        caps.add("external_binary")
                    elif target.id == "ctypes" and attr in _CTYPES_DANGEROUS:
                        caps.add("native_api")
        elif isinstance(node, ast.keyword):
            # shell=True / shell=1 / shell="x" 等真值都视为启用 shell（之前只认 is True，
            # 会漏掉 shell=1 这类真值）。shell=False/0 不触发。
            if node.arg == "shell" and isinstance(node.value, ast.Constant) and node.value.value:
                caps.add("external_binary")
        elif isinstance(node, ast.Constant):
            # "runas" 动词出现在任意字符串里（ShellExecuteW 的 verb、os.startfile(x,"runas")、
            # 或 PowerShell 命令串里的 Start-Process -Verb RunAs）都是自行提权信号。
            if isinstance(node.value, str) and _ELEVATION_VERB in node.value.lower():
                caps.add("self_elevation")
    return caps


# ============================================================================
# Ed25519 签名校验（仅 builtin 插件）
# ============================================================================

def verify_plugin_sig(plugin_dir: str, origin: str = "builtin") -> bool:
    """校验 builtin 插件目录的 signature.sig。

    用户插件（origin != "builtin"）直接放行，不查签名。
    """
    if origin != "builtin":
        return True
    try:
        from notmyfault.signing_keys import get_public_keys
        pub_keys = get_public_keys()
        if not pub_keys:
            return False
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pubs = [Ed25519PublicKey.from_public_bytes(k) for k in pub_keys]
    except ImportError:
        return False
    sig_file = os.path.join(plugin_dir, "signature.sig")
    if not os.path.exists(sig_file):
        return False
    with open(sig_file, "rb") as f:
        sig = f.read()
    # 必须与 signing.sign_plugin 用同一份文件清单（见 signing.plugin_files），
    # 否则两边 payload 不一致会让合法签名校验失败。
    from notmyfault.signing import plugin_files
    payload = b"".join(f.read_bytes() for f in plugin_files(plugin_dir))
    digest = hashlib.sha256(payload).digest()
    for pub in pubs:
        try:
            pub.verify(sig, digest)
            return True
        except Exception:
            continue
    return False


# ============================================================================
# hash 清单完整性校验
# ============================================================================

_PLUGIN_MANIFEST_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "plugin_manifest.json")


def compute_file_hash(file_path: str) -> str | None:
    """计算文件 SHA-256；读取失败返回 None。"""
    try:
        with open(file_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def load_plugin_manifest() -> dict[str, dict[str, str]]:
    """加载插件 hash 清单（不存在/损坏时返回空 dict）。"""
    try:
        if os.path.exists(_PLUGIN_MANIFEST_FILE):
            with open(_PLUGIN_MANIFEST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_plugin_manifest(manifest: dict[str, dict[str, str]]) -> None:
    """原子写入插件 hash 清单（失败静默）。

    tmp + os.replace 原子替换，避免写一半崩溃留下损坏的清单；替换失败（Windows
    下目标被占用）再退回直接写。清单是可重建的缓存，损坏最坏只是下次重新哈希。
    """
    try:
        os.makedirs(os.path.dirname(_PLUGIN_MANIFEST_FILE), exist_ok=True)
        tmp_path = _PLUGIN_MANIFEST_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, sort_keys=True)
            os.replace(tmp_path, _PLUGIN_MANIFEST_FILE)
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            with open(_PLUGIN_MANIFEST_FILE, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, sort_keys=True)
    except OSError:
        pass


def verify_plugin_integrity(
    plugin_id: str, files: List[Tuple[str, str]]
) -> Tuple[bool, str]:
    """校验插件文件是否与清单一致；首次记录则写入清单。"""
    manifest = load_plugin_manifest()
    existing = manifest.get(plugin_id, {})
    all_match = True
    messages: List[str] = []
    for file_type, file_path in files:
        current_hash = compute_file_hash(file_path)
        if current_hash is None:
            messages.append("无法读取 " + file_type)
            all_match = False
            continue
        if plugin_id in manifest:
            expected_hash = existing.get(file_type)
            if expected_hash is not None and current_hash != expected_hash:
                messages.append(file_type + " 文件已被修改！（期望 " + expected_hash[:12] + "...）")
                all_match = False
        if plugin_id not in manifest:
            manifest[plugin_id] = {}
        manifest[plugin_id][file_type] = current_hash
    save_plugin_manifest(manifest)
    if not all_match:
        return False, "；".join(messages)
    return True, "完整性校验通过"
