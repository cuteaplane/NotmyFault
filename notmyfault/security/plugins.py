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
    """扫描 .py 源码是否 import 了 notmyfault.security.sudo（AST 级别检查）。

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
                    if alias.name == "notmyfault.security.sudo":
                        return True
            elif isinstance(node, ast.ImportFrom):
                if node.module == "notmyfault.security.sudo":
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
# 插件必须走 notmyfault.security.sudo.run_as_admin（声明 admin 权限），禁止自己直接提权。
_ELEVATION_FUNCS = {"shellexecute", "shellexecutew", "shellexecuteex", "shellexecutea"}
_ELEVATION_VERB = "runas"
# 动态执行函数：exec/eval/compile 可绕过所有 AST 能力检测，
# __import__ 和 importlib.import_module 可动态导入任意模块。
# 这些一律禁止（PoC-5 修复），声明了也不允许。
_DYNAMIC_EXEC_FUNCS = {"exec", "eval", "compile"}


def scan_plugin_capabilities(py_file_path: str) -> Set[str]:
    """返回插件源码使用的危险能力集合（未声明则加载时告警/拒绝）。

    检测两类绕过行为（需在清单 permissions 中声明）：
    - native_api: 直接调用原生系统 API（ctypes/windll/win32api 等），
      可绕过 sudo.run_as_admin 自行提权或操作系统底层。
    - external_binary: 执行外部二进制/进程（subprocess、os.system、os.popen、
      os.startfile、os.exec*/spawn*、shell=True）。

    admin 能力（import notmyfault.security.sudo）由上面的 check_sudo_import 负责，
    此处不重复。扫描在插件 exec 之前进行，避免执行未声明的危险代码。

    另外 self_elevation（自行提权）是一律禁止的能力，不可声明：
    - 检测 ShellExecute/ShellExecuteW/ShellExecuteEx 等调用，或任意字符串里出现
      "runas" 动词（UAC 提权）。插件要提权必须走 notmyfault.security.sudo.run_as_admin
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

    # 先收集常见 import 别名。只比较 AST 中的裸名字会漏掉
    # ``import os as system``、``importlib as il`` 等最普通的绕过。
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
        elif isinstance(node, ast.Attribute):
            base, attr = node.value, node.attr
            # ShellExecute/ShellExecuteW/ShellExecuteEx 等 UAC 提权 API（无论挂在哪个对象上）。
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
        elif isinstance(node, ast.Call):
            func = node.func
            # 直接 ShellExecuteW(...)（from xx import ShellExecuteW 之后）。
            if isinstance(func, ast.Name) and func.id.lower() in _ELEVATION_FUNCS:
                caps.add("self_elevation")
            # exec/eval/compile 动态执行（PoC-5 修复）- 一律禁止
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
                    elif owner in ("builtins", "__builtins__") and attr == "__import__":
                        caps.add("dynamic_exec")
                    elif owner == "importlib" and attr == "import_module":
                        caps.add("dynamic_exec")
            # __import__ 调用本身（无论参数是否常量）都视为动态执行
            if isinstance(func, ast.Name) and func.id == "__import__":
                caps.add("dynamic_exec")
            # importlib.import_module 调用本身（无论参数是否常量）
            if (isinstance(func, ast.Attribute) and func.attr == "import_module"
                  and isinstance(func.value, ast.Name)
                  and module_aliases.get(func.value.id, func.value.id) == "importlib"):
                caps.add("dynamic_exec")
            # __import__("subprocess") / importlib.import_module("ctypes") 常量模块名检测
            # （保留：进一步标注 native_api/external_binary，方便诊断）
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
            # getattr(os, "system") / getattr(subprocess, "Popen") 动态属性取用。
            # getattr(__builtins__, "__import__") 也视为动态执行。
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
                        elif owner in ("__builtins__", "builtins") and attr == "__import__":
                            caps.add("dynamic_exec")
                    elif isinstance(target, ast.Subscript):
                        # getattr(sys.modules['os'], 'system') 绕过：
                        # 通过 sys.modules 下标取模块后动态取属性。
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
        elif isinstance(node, ast.Subscript):
            # __builtins__['exec'] / __builtins__['__import__'] 下标取内置函数。
            tv = node.value
            if (
                isinstance(tv, ast.Name)
                and tv.id in ("__builtins__", "builtins")
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and node.slice.value in _DYNAMIC_EXEC_FUNCS
            ):
                caps.add("dynamic_exec")
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
# AST: 借壳提权模式扫描（安装预览 / 加载时告警）
# ============================================================================

_PLUGIN_MODULE_PREFIXES = ("notmyfault.action_", "notmyfault.trigger_")
_DYNAMIC_EXEC_BYPASS = {"exec", "eval", "compile", "__import__"}


def scan_borrowed_privilege(py_file_path: str) -> list[str]:
    """扫描插件源码中可能"借壳"其他已授权插件身份的访问模式。

    借壳攻击：未授权插件导入/访问引擎已加载的插件模块
    （notmyfault.action_* / notmyfault.trigger_*），调用其内部函数，
    使 sudo.run_as_admin 的调用栈检测命中已授权插件。

    命中以下模式不代表一定提权，但属于高风险信号：
    - 直接 import / from-import 引擎插件模块
    - 通过 sys.modules 取引擎插件模块
    - 通过 getattr(__builtins__, ...) 获取 exec/eval/compile/__import__
      （绕过 AST 动态执行检测）
    - 调用已导入插件模块的内部函数

    安装预览（api_server.plugin_preview）与插件加载（plugin_loader）时
    应据此向用户告警。
    """
    findings: list[str] = []
    try:
        with open(py_file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception:
        return findings

    # 别名 -> 完整模块名（import notmyfault.action_x as bt -> bt 指向完整名；
    # from notmyfault.action_x import f -> f 指向 "notmyfault.action_x.f"）
    module_aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # 只记录带 asname 的点分模块（bt = notmyfault.action_x）；
                # 裸 `import notmyfault.action_x` 不映射顶级包名，否则之后
                # 任何 notmyfault.xxx 用法都会被误判成引用该插件模块。
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
            # 污点跟踪：bt = sys.modules['notmyfault.action_x'] 之后 bt 指向插件模块
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
        # 1) import notmyfault.action_xxx / from notmyfault.action_xxx import ...
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

        # 2) sys.modules['notmyfault.action_xxx'] 动态取模块
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

        # 3) getattr(__builtins__, 'exec'/'eval'/...) 绕过动态执行检测
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

        # 4) 调用已导入插件模块的内部函数：alias.func(...) / 完整链 / from-import 后 func(...)
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

    # 去重并保持稳定顺序
    return sorted(set(findings))


# ============================================================================
# Ed25519 签名校验（builtin + user 插件均需校验）
# ============================================================================

def verify_plugin_sig(plugin_dir: str, origin: str = "builtin") -> bool:
    """校验插件目录的 signature.sig。

    所有插件（builtin 和 user）都需要签名校验。
    用户插件通过 /api/plugins/install 安装时会用项目私钥签名，
    直接放入用户插件目录的插件（无签名）将被拒载。
    """
    try:
        from notmyfault.security.signing_keys import get_public_keys
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
    from notmyfault.security.signing import plugin_files
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
