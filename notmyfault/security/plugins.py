"""提供 PluginLoader 调用的插件导入检查、能力扫描、签名和完整性校验"""
import ast
import hashlib
import json
import os
import tempfile
from typing import List, Optional, Set, Tuple

from notmyfault.config import CONFIG_FILE


def check_sudo_import(py_file_path: str) -> bool:
    """用 AST 检查源码是否导入 notmyfault.security.sudo。"""
    try:
        with open(py_file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in (
                        "notmyfault.security.sudo",
                        "notmyfault.security",
                    ):
                        return True
            elif isinstance(node, ast.ImportFrom):
                if node.module == "notmyfault.security.sudo":
                    return True
                if node.module == "notmyfault":
                    for alias in node.names:
                        if alias.name == "sudo":
                            return True
                if node.module == "notmyfault.security":
                    for alias in node.names:
                        if alias.name == "sudo":
                            return True
        return False
    except Exception:
        return False


_NATIVE_MODULES = {"ctypes", "win32api", "win32con", "pywintypes", "_winapi"}
_EXTERNAL_MODULES = {"subprocess"}
_OS_DANGEROUS = {
    "system", "popen", "startfile",
    "execv", "execve", "execl", "execlp", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
}
_SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
_CTYPES_DANGEROUS = {"windll", "CDLL", "WinDLL", "OleDLL", "WINFUNCTYPE", "CFUNCTYPE", "Structure"}
# ShellExecute 系列和 runas 字符串表示插件自行提权。
_ELEVATION_FUNCS = {"shellexecute", "shellexecutew", "shellexecuteex", "shellexecutea"}
_ELEVATION_VERB = "runas"
# exec、eval、compile、__import__ 和 importlib.import_module 归为 dynamic_exec
_DYNAMIC_EXEC_FUNCS = {"exec", "eval", "compile"}
_BUILTIN_OWNERS = {"builtins", "__builtins__"}


def scan_plugin_capabilities(py_file_path: str) -> Set[str]:
    """用 AST 记录插件使用的 native_api、external_binary、self_elevation 和 dynamic_exec"""
    caps: Set[str] = set()
    try:
        with open(py_file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception:
        return caps

    # 同时记录 import 别名，覆盖 os as system 和 importlib as il。
    module_aliases: dict[str, str] = {}
    imported_symbols: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                module_aliases[alias.asname or top] = top
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            for alias in node.names:
                imported_symbols[alias.asname or alias.name] = (top, alias.name)

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
            if top in _BUILTIN_OWNERS:
                for alias in node.names:
                    if alias.name in _DYNAMIC_EXEC_BYPASS:
                        caps.add("dynamic_exec")
        elif isinstance(node, ast.Attribute):
            base, attr = node.value, node.attr
            # ShellExecute 系列调用表示自行提权。
            if attr.lower() in _ELEVATION_FUNCS:
                caps.add("self_elevation")
            if isinstance(base, ast.Name):
                owner = module_aliases.get(base.id, base.id)
                if owner == "os" and attr in _OS_DANGEROUS:
                    caps.add("external_binary")
                if owner == "subprocess" and attr in _SUBPROCESS_CALLS:
                    caps.add("external_binary")
                if owner == "ctypes" and attr in _CTYPES_DANGEROUS:
                    caps.add("native_api")
                # builtins.eval / __builtins__.exec 这类属性访问同样是动态执行。
                if owner in _BUILTIN_OWNERS and attr in _DYNAMIC_EXEC_FUNCS:
                    caps.add("dynamic_exec")
        elif isinstance(node, ast.Call):
            func = node.func
            # 直接导入后的 ShellExecuteW 调用也要标记。
            if isinstance(func, ast.Name) and func.id.lower() in _ELEVATION_FUNCS:
                caps.add("self_elevation")
            # exec、eval 和 compile 属于动态执行。
            if isinstance(func, ast.Name) and func.id in _DYNAMIC_EXEC_FUNCS:
                caps.add("dynamic_exec")
            if isinstance(func, ast.Name):
                imported = imported_symbols.get(func.id)
                if imported:
                    owner, attr = imported
                    if owner == "os" and attr in _OS_DANGEROUS:
                        caps.add("external_binary")
                    elif owner == "subprocess" and attr in _SUBPROCESS_CALLS:
                        caps.add("external_binary")
                    elif owner == "ctypes" and attr in _CTYPES_DANGEROUS:
                        caps.add("native_api")
                    elif owner in _BUILTIN_OWNERS and attr in _DYNAMIC_EXEC_BYPASS:
                        caps.add("dynamic_exec")
                    elif owner == "importlib" and attr == "import_module":
                        caps.add("dynamic_exec")
            # __import__ 调用本身就是动态执行。
            if isinstance(func, ast.Name) and func.id == "__import__":
                caps.add("dynamic_exec")
            # importlib.import_module 调用本身就是动态执行。
            if (isinstance(func, ast.Attribute) and func.attr == "import_module"
                  and isinstance(func.value, ast.Name)
                  and module_aliases.get(func.value.id, func.value.id) == "importlib"):
                caps.add("dynamic_exec")
            # 常量模块名还会补充 native_api 或 external_binary 标记。
            mod_arg = None
            if isinstance(func, ast.Name) and func.id == "__import__":
                mod_arg = node.args[0] if node.args else None
            elif (isinstance(func, ast.Attribute) and func.attr == "import_module"
                  and isinstance(func.value, ast.Name)
                  and module_aliases.get(func.value.id, func.value.id) == "importlib"):
                mod_arg = node.args[0] if node.args else None
            if isinstance(mod_arg, ast.Constant) and isinstance(mod_arg.value, str):
                top = mod_arg.value.split(".")[0]
                if top in _NATIVE_MODULES:
                    caps.add("native_api")
                if top in _EXTERNAL_MODULES:
                    caps.add("external_binary")
            # getattr() 取危险属性或动态执行函数也要标记。
            if (isinstance(func, ast.Name) and func.id == "getattr"
                    and len(node.args) >= 2):
                target, name_arg = node.args[0], node.args[1]
                if isinstance(name_arg, ast.Constant) \
                        and isinstance(name_arg.value, str):
                    attr = name_arg.value
                    if isinstance(target, ast.Name):
                        owner = module_aliases.get(target.id, target.id)
                        if owner == "os" and attr in _OS_DANGEROUS:
                            caps.add("external_binary")
                        elif owner == "subprocess" and attr in _SUBPROCESS_CALLS:
                            caps.add("external_binary")
                        elif owner == "ctypes" and attr in _CTYPES_DANGEROUS:
                            caps.add("native_api")
                        elif owner in _BUILTIN_OWNERS and attr in _DYNAMIC_EXEC_BYPASS:
                            caps.add("dynamic_exec")
                    elif isinstance(target, ast.Subscript):
                        # 通过 sys.modules 取模块后再动态调用 getattr() 也要标记。
                        tv = target.value
                        if (
                            isinstance(tv, ast.Attribute)
                            and tv.attr == "modules"
                            and isinstance(tv.value, ast.Name)
                            and tv.value.id == "sys"
                            and isinstance(target.slice, ast.Constant)
                            and isinstance(target.slice.value, str)
                        ):
                            mod = target.slice.value.split(".")[0]
                            if mod == "os" and attr in _OS_DANGEROUS:
                                caps.add("external_binary")
                            elif mod == "subprocess" and attr in _SUBPROCESS_CALLS:
                                caps.add("external_binary")
                            elif mod in _NATIVE_MODULES and attr in _CTYPES_DANGEROUS:
                                caps.add("native_api")
                            elif mod in _BUILTIN_OWNERS and attr in _DYNAMIC_EXEC_BYPASS:
                                caps.add("dynamic_exec")
        elif isinstance(node, ast.Subscript):
            # 通过 __builtins__ 下标取动态执行函数也要标记。
            tv = node.value
            if (
                isinstance(tv, ast.Name)
                and tv.id in _BUILTIN_OWNERS
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and node.slice.value in _DYNAMIC_EXEC_BYPASS
            ):
                caps.add("dynamic_exec")
        elif isinstance(node, ast.keyword):
            # shell 参数为真值时标记 external_binary。
            if node.arg == "shell" and isinstance(node.value, ast.Constant) and node.value.value:
                caps.add("external_binary")
        elif isinstance(node, ast.Constant):
            # 只认恰好等于 runas 的字符串，包含子串的文案不该拒载插件。
            if isinstance(node.value, str) and node.value.strip().lower() == _ELEVATION_VERB:
                caps.add("self_elevation")
    return caps


_PLUGIN_MODULE_PREFIXES = ("notmyfault.action_", "notmyfault.trigger_")
_DYNAMIC_EXEC_BYPASS = {"exec", "eval", "compile", "__import__"}


def scan_borrowed_privilege(py_file_path: str) -> list[str]:
    """检查插件是否调用已加载的插件模块或动态执行函数"""
    findings: list[str] = []
    try:
        with open(py_file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception:
        return findings

    # 记录别名指向的完整模块名。
    module_aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # 带别名的点分模块才进入映射表，普通 notmyfault 模块不进入映射表。
                if alias.asname:
                    module_aliases[alias.asname] = alias.name
                elif "." not in alias.name:
                    module_aliases[alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0] if mod else ""
            for alias in node.names:
                target = f"{mod}.{alias.name}" if mod else alias.name
                module_aliases[alias.asname or alias.name] = (
                    target if top.startswith("notmyfault") else top or alias.name
                )
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            # 记录从 sys.modules 取出的插件模块别名。
            val = node.value
            if (
                isinstance(val, ast.Subscript)
                and isinstance(val.value, ast.Attribute)
                and val.value.attr == "modules"
                and isinstance(val.value.value, ast.Name)
                and val.value.value.id == "sys"
                and isinstance(val.slice, ast.Constant)
                and isinstance(val.slice.value, str)
                and val.slice.value.startswith(_PLUGIN_MODULE_PREFIXES)
            ):
                module_aliases[node.targets[0].id] = val.slice.value

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_PLUGIN_MODULE_PREFIXES):
                    findings.append(
                        f"直接导入引擎插件模块 {alias.name}：可借壳其管理员授权"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith(_PLUGIN_MODULE_PREFIXES):
                names = ", ".join(alias.name for alias in node.names)
                findings.append(
                    f"from-import 引擎插件模块 {mod} 的 {names}：可借壳其管理员授权"
                )

        if isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "modules"
                and isinstance(value.value, ast.Name)
                and value.value.id == "sys"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and node.slice.value.startswith(_PLUGIN_MODULE_PREFIXES)
            ):
                findings.append(
                    f"通过 sys.modules 获取引擎插件模块 {node.slice.value}"
                )

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in ("__builtins__", "builtins")
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value in _DYNAMIC_EXEC_BYPASS
        ):
            findings.append(
                f"通过 getattr 获取内置动态执行函数 {node.args[1].value}："
                "可绕过动态执行检测并在任意命名空间执行代码"
            )

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                chain_parts = []
                chain_node = func
                while isinstance(chain_node, ast.Attribute):
                    chain_parts.append(chain_node.attr)
                    chain_node = chain_node.value
                if isinstance(chain_node, ast.Name):
                    chain_parts.append(chain_node.id)
                chain = ".".join(reversed(chain_parts))
                first, dot, rest = chain.partition(".")
                resolved = module_aliases.get(first, first) + ("." + rest if dot else "")
                if resolved.startswith(_PLUGIN_MODULE_PREFIXES):
                    findings.append(
                        f"调用引擎插件模块内部函数 {resolved}()"
                    )
            elif isinstance(func, ast.Name):
                target = module_aliases.get(func.id)
                if (
                    target is not None
                    and target.startswith(_PLUGIN_MODULE_PREFIXES)
                    and "." in target
                ):
                    findings.append(
                        f"调用引擎插件模块内部函数 {target}()"
                    )

    return sorted(set(findings))


def _verify_sig_with_keys(plugin_dir: str, pubs) -> bool:
    """用给定公钥列表校验 signature.sig，清单与签名时保持同一份。"""
    sig_file = os.path.join(plugin_dir, "signature.sig")
    if not os.path.exists(sig_file):
        return False
    try:
        with open(sig_file, "rb") as f:
            sig = f.read()
        from notmyfault.security.signing import plugin_files
        payload = b"".join(f.read_bytes() for f in plugin_files(plugin_dir))
    except OSError:
        return False
    digest = hashlib.sha256(payload).digest()
    for pub in pubs:
        try:
            pub.verify(sig, digest)
            return True
        except Exception:
            continue
    return False


def plugin_signature_kind(plugin_dir: str, origin: str = "builtin") -> str:
    """返回签名来源：official、author、official-legacy 或 none，验签失败一律算 none"""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:
        return "none"

    if origin == "builtin":
        try:
            from notmyfault.security.signing_keys import get_public_keys
            pub_keys = get_public_keys()
            if not pub_keys:
                return "none"
            pubs = [Ed25519PublicKey.from_public_bytes(k) for k in pub_keys]
        except Exception:
            return "none"
        return "official" if _verify_sig_with_keys(plugin_dir, pubs) else "none"

    # 非内置插件由作者自签，公钥随插件目录分发，验的是没被篡改过。
    key_path = os.path.join(plugin_dir, "public_key.pem")
    if os.path.exists(key_path):
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            with open(key_path, "rb") as f:
                pub = load_pem_public_key(f.read())
        except Exception:
            return "none"
        return "author" if _verify_sig_with_keys(plugin_dir, [pub]) else "none"

    if origin == "third_party":
        return "none"

    # 旧安装流程用本地密钥代签的用户插件目录里没有 public_key.pem，退回官方和用户公钥表验签。
    try:
        from notmyfault.security.signing_keys import get_public_keys
        pub_keys = get_public_keys()
        if not pub_keys:
            return "none"
        pubs = [Ed25519PublicKey.from_public_bytes(k) for k in pub_keys]
    except Exception:
        return "none"
    return "official-legacy" if _verify_sig_with_keys(plugin_dir, pubs) else "none"


def verify_plugin_sig(plugin_dir: str, origin: str = "builtin") -> bool:
    """校验插件签名文件和签名覆盖的文件清单，任何异常都按验签失败处理"""
    return plugin_signature_kind(plugin_dir, origin) != "none"


_PLUGIN_MANIFEST_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "plugin_manifest.json")


def compute_file_hash(file_path: str) -> str | None:
    """计算文件 SHA-256，读取失败时返回 None"""
    try:
        with open(file_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def load_plugin_manifest() -> dict[str, dict[str, str]]:
    """加载插件 hash 清单，文件不存在或损坏时返回空字典。"""
    try:
        if os.path.exists(_PLUGIN_MANIFEST_FILE):
            with open(_PLUGIN_MANIFEST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_plugin_manifest(manifest: dict[str, dict[str, str]]) -> bool:
    """用同目录临时文件原子写入插件哈希清单"""
    manifest_dir = os.path.dirname(_PLUGIN_MANIFEST_FILE) or "."
    tmp_path: str | None = None
    fd = -1
    try:
        os.makedirs(manifest_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(_PLUGIN_MANIFEST_FILE) + ".",
            suffix=".tmp",
            dir=manifest_dir,
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _PLUGIN_MANIFEST_FILE)
        tmp_path = None
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def verify_plugin_integrity(
    plugin_id: str, files: List[Tuple[str, str]]
) -> Tuple[bool, str]:
    """校验插件文件与清单的一致性并记录首次哈希"""
    manifest = load_plugin_manifest()
    has_existing = plugin_id in manifest
    existing = manifest.get(plugin_id, {})
    current: dict[str, str] = {}
    present_files: set[str] = set()
    messages: List[str] = []
    for file_type, file_path in files:
        present_files.add(file_type)
        current_hash = compute_file_hash(file_path)
        if current_hash is None:
            messages.append("无法读取 " + file_type)
            continue
        current[file_type] = current_hash

    if has_existing:
        for file_type in sorted(existing.keys() - present_files):
            messages.append(file_type + " 文件已被删除")
        for file_type in sorted(present_files - existing.keys()):
            messages.append(file_type + " 文件为清单外新增")
        for file_type in sorted(existing.keys() & current.keys()):
            expected_hash = existing[file_type]
            if current[file_type] == expected_hash:
                continue
            messages.append(file_type + " 文件已被修改！（期望 " + expected_hash[:12] + "...）")

    if messages:
        return False, "；".join(messages)
    if not has_existing:
        manifest[plugin_id] = current
        if not save_plugin_manifest(manifest):
            return False, "无法保存完整性清单"
    return True, "完整性校验通过"
