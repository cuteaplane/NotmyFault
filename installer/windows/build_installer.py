import argparse
import ast
from contextlib import nullcontext
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SOURCES = Path(__file__).resolve().parent
APP_FILES = (
    "build.py", "dashboard.pyw", "NOTMYFAULT.pyw", "nmf.py",
    "logo.ico", "logo.png", "LICENSE", "requirements.txt",
)
SKIP_NAMES = {"__pycache__", ".git", ".private"}


class BuildError(RuntimeError):
    pass


def copy_tree(source, destination, excluded=()):
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        if item.name in SKIP_NAMES or item.name in excluded:
            continue
        if item.suffix.lower() in {".pyc", ".pyo"}:
            continue
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise BuildError("构建输入不能包含目录链接：" + str(item))
        target = destination / item.name
        if item.is_dir():
            copy_tree(item, target, excluded)
        elif item.is_file():
            shutil.copy2(item, target)


def subprocess_debug_context():
    # pydevd 的子进程注入会在 Python 3.14 下误报模块缺失并返回 0。
    debugger = sys.modules.get("pydevd")
    skip_patch = getattr(debugger, "skip_subprocess_arg_patch", None)
    return skip_patch() if skip_patch else nullcontext()


class Runner:
    def __init__(self, work):
        self.log_path = work / "build.log"
        temporary = work / "temp"
        temporary.mkdir(parents=True, exist_ok=True)
        self.environment = os.environ.copy()
        self.environment.update({
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "TEMP": str(temporary), "TMP": str(temporary),
            "PIP_CACHE_DIR": str(temporary / "pip-cache"),
            "npm_config_cache": str(temporary / "npm-cache"),
        })
        self.environment.pop("PYTHONHOME", None)
        self.environment.pop("PYTHONPATH", None)
        self.environment.pop("NOTMYFAULT_SIGNING_PASSPHRASE", None)

    def run(self, command, cwd=ROOT, extra_environment=None):
        arguments = [str(value) for value in command]
        environment = self.environment.copy()
        if extra_environment:
            environment.update(extra_environment)
        print("运行：" + subprocess.list2cmdline(arguments), flush=True)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write("\n" + subprocess.list2cmdline(arguments) + "\n")
            with subprocess_debug_context():
                process = subprocess.Popen(
                    arguments, cwd=str(cwd), env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                )
            with process:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                code = process.wait()
            if code:
                raise BuildError("命令退出码为 %s，日志：%s" % (code, self.log_path))


def inspect_python(executable):
    located = shutil.which(executable) or executable
    candidate = Path(located).expanduser().resolve()
    probe = (
        "import ensurepip,json,struct,sys,sysconfig,venv; "
        "print(json.dumps({'base':sys.base_prefix,'version':list(sys.version_info[:3]),"
        "'platform':sysconfig.get_platform(),'bits':struct.calcsize('P')*8,"
        "'implementation':sys.implementation.name}))"
    )
    with subprocess_debug_context():
        result = subprocess.run(
            [str(candidate), "-I", "-c", probe], capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=True,
        )
    info = json.loads(result.stdout.strip())
    if (info["platform"] != "win-amd64" or info["bits"] != 64
            or info["implementation"] != "cpython"
            or tuple(info["version"]) < (3, 11)):
        raise BuildError("构建需要带有 venv 和 ensurepip 的 Windows x64 CPython 3.11+。")
    base = Path(info["base"]).resolve()
    if (base / "conda-meta").is_dir():
        raise BuildError("Conda 的运行时依赖其他目录，请用 --python 指定 python.org 安装的 CPython。")
    for required in ("python.exe", "pythonw.exe", "Lib", "DLLs"):
        if not (base / required).exists():
            raise BuildError("Python 基础运行时缺少：" + str(base / required))
    return candidate, base, info


def version_string():
    tree = ast.parse((ROOT / "notmyfault" / "version.py").read_text(encoding="utf-8"))
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            if any(isinstance(item, ast.Name) and item.id == "__version__"
                   for item in statement.targets):
                value = ast.literal_eval(statement.value)
                if isinstance(value, str) and value and all(
                    character.isalnum() or character in ".-_()" for character in value
                ):
                    return value
    raise BuildError("notmyfault/version.py 中的版本号无效。")


def prepare_app(app):
    app.mkdir(parents=True)
    copy_tree(ROOT / "notmyfault", app / "notmyfault", {"tests", "simulator"})
    copy_tree(ROOT / "Win_toaster", app / "Win_toaster")
    for name in APP_FILES:
        shutil.copy2(ROOT / name, app / name)


def prepare_frontend(work, app, runner, node, npm):
    frontend = work / "frontend" / "dashboard"
    copy_tree(ROOT / "dashboard", frontend, {"node_modules", "dist", "tests"})
    version_dir = frontend.parent / "notmyfault"
    version_dir.mkdir()
    shutil.copy2(ROOT / "notmyfault" / "version.py", version_dir / "version.py")
    shutil.copy2(ROOT / "logo.png", frontend.parent / "logo.png")
    runner.run([npm, "ci", "--no-audit", "--no-fund"], frontend)
    runner.run([
        node, frontend / "node_modules" / "vite" / "bin" / "vite.js",
        "build", "--outDir", app / "dashboard" / "dist",
    ], frontend)
    if not (app / "dashboard" / "dist" / "index.html").is_file():
        raise BuildError("Dashboard 构建没有生成 index.html。")


def prepare_runtime(base, runtime):
    runtime.mkdir()
    for name in ("python.exe", "pythonw.exe"):
        shutil.copy2(base / name, runtime / name)
    for item in sorted(base.iterdir()):
        if item.is_file() and (item.suffix.lower() == ".dll"
                               or item.name.upper().startswith("LICENSE")):
            shutil.copy2(item, runtime / item.name)
    copy_tree(base / "DLLs", runtime / "DLLs", {"test", "tests"})
    copy_tree(base / "Lib", runtime / "Lib", {
        "site-packages", "test", "tests", "idlelib", "turtledemo",
    })
    if (base / "tcl").is_dir():
        copy_tree(base / "tcl", runtime / "tcl", {"test", "tests", "demos"})


def sign_app(app, python, runner, development, passphrase):
    private = app / ".private"
    private.mkdir()
    environment = {"NOTMYFAULT_SIGNING_PASSPHRASE": passphrase}
    try:
        if development:
            runner.run([
                python, "build.py", "init-keys", "--builtin", "--encrypt",
            ], app, environment)
        else:
            shutil.copy2(
                ROOT / ".private" / "signing_private_key.pem",
                private / "signing_private_key.pem",
            )
        runner.run([python, "build.py", "build", "--security-mode=strict"], app, environment)
    finally:
        for name in ("signing_private_key.pem", "signing_public.pem"):
            (private / name).unlink(missing_ok=True)
        private.rmdir()
    runner.run([python, "build.py", "verify"], app)


def make_payload(stage, output):
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for directory in ("app", "runtime", "wheels"):
            for item in sorted((stage / directory).rglob("*")):
                relative = item.relative_to(stage)
                if ".private" in relative.parts:
                    raise BuildError("安装包中出现了私钥目录：" + str(relative))
                if "__pycache__" in relative.parts or item.suffix.lower() in {".pyc", ".pyo"}:
                    continue
                if item.is_file():
                    archive.write(item, relative.as_posix())


def compiler_path():
    framework = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    compiler = framework / "csc.exe"
    if not compiler.is_file():
        raise BuildError("未找到 .NET Framework 4 的 x64 C# 编译器：" + str(compiler))
    return compiler


def prepare_fonts(work, python, runner):
    font_directory = work / "fonts"
    runner.run([python, SOURCES / "prepare_fonts.py", font_directory])
    return font_directory


def compile_installer(payload, version_file, executable, runner):
    compiler = compiler_path()
    framework = compiler.parent
    references = [framework / "WPF" / (name + ".dll") for name in (
        "PresentationCore", "PresentationFramework", "WindowsBase",
    )]
    references.extend(framework / (name + ".dll") for name in (
        "System.Xaml", "System.Drawing", "System.Windows.Forms",
        "System.IO.Compression", "System.IO.Compression.FileSystem", "Microsoft.CSharp",
    ))
    font_directory = payload.parent / "fonts"
    fonts = [font_directory / name for name in (
        "GoogleSansFlex-SemiBold.ttf", "Roboto-Regular.ttf", "Roboto-SemiBold.ttf",
    )]
    if not all(font.is_file() for font in fonts):
        raise BuildError("缺少安装器字体，请先用 prepare_fonts 生成字体资源。")
    resource_packer = payload.parent / "PackResources.exe"
    runner.run([
        compiler, "/nologo", "/target:exe", "/platform:x64", "/optimize+",
        "/out:" + str(resource_packer), SOURCES / "PackResources.cs",
    ])
    font_resources = payload.parent / "InstallerFonts.g.resources"
    font_licenses = sorted((SOURCES / "fonts").glob("*-OFL.txt"))
    runner.run([resource_packer, font_resources, *fonts, *font_licenses])
    common_command = [
        compiler, "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/utf8output",
        "/win32manifest:" + str(SOURCES / "app.manifest"),
        "/win32icon:" + str(ROOT / "logo.ico"),
        "/resource:" + str(version_file) + ",NotmyFault.Version",
        "/resource:" + str(ROOT / "logo.png") + ",NotmyFault.Logo",
        "/resource:" + str(ROOT / "LICENSE") + ",NotmyFault.License",
    ]
    common_command.extend("/reference:" + str(reference) for reference in references)
    sources = [SOURCES / name for name in (
        "InstallerWindow.cs", "MotionScene.cs", "InstallEngine.cs",
        "InstallMaintenance.cs", "UninstallerWindow.cs",
    )]
    uninstaller = executable.parent / "NotmyFault-Uninstall.exe"
    runner.run(common_command + [
        "/out:" + str(uninstaller), "/main:NotmyFault.Setup.UninstallProgram",
        "/resource:" + str(font_resources) + "," + uninstaller.stem + ".g.resources",
    ] + sources)
    runner.run(common_command + [
        "/out:" + str(executable), "/main:NotmyFault.Setup.Program",
        "/resource:" + str(payload) + ",NotmyFault.Payload.zip",
        "/resource:" + str(uninstaller) + ",NotmyFault.Uninstaller.exe",
        "/resource:" + str(font_resources) + "," + executable.stem + ".g.resources",
    ] + sources)


def arguments():
    parser = argparse.ArgumentParser(description="构建 NotmyFault Windows x64 离线安装器。")
    parser.add_argument("--python", default=sys.executable, help="用于构建及随包分发的 Python 解释器")
    parser.add_argument("--output", type=Path, default=SOURCES / "dist", help="安装器输出目录，默认 installer/windows/dist")
    parser.add_argument("--development-key", action="store_true", help="仅在暂存目录生成开发签名密钥，输出标记为开发构建")
    parser.add_argument("--non-interactive", action="store_true", help="禁止密码提示，项目密钥密码须由 NOTMYFAULT_SIGNING_PASSPHRASE 提供")
    return parser.parse_args()


def main():
    args = arguments()
    if os.name != "nt":
        raise BuildError("安装器需要在 Windows x64 上构建。")
    output = args.output.expanduser().resolve()
    node = shutil.which("node")
    npm = shutil.which("npm.cmd")
    if not node or not npm:
        raise BuildError("构建需要安装 Node.js 和 npm，并将它们加入 PATH。")
    compiler_path()
    python, base, python_info = inspect_python(args.python)
    version = version_string()
    if args.development_key:
        passphrase = secrets.token_urlsafe(48)
        version += "-development"
        print("开发构建：安装包将使用本次生成的开发公钥。", flush=True)
    else:
        key = ROOT / ".private" / "signing_private_key.pem"
        if not key.is_file():
            raise BuildError("缺少项目签名私钥；本地试装可显式使用 --development-key。")
        print("构建将使用项目现有私钥：" + str(key), flush=True)
        print("已有加密私钥需要原密码；安装时会另行设置签名密码。", flush=True)
        passphrase = os.environ.get("NOTMYFAULT_SIGNING_PASSPHRASE", "")
        if passphrase:
            print("签名密码读取自 NOTMYFAULT_SIGNING_PASSPHRASE。", flush=True)
        else:
            if args.non_interactive or not sys.stdin.isatty():
                raise BuildError("请设置 NOTMYFAULT_SIGNING_PASSPHRASE，或在交互终端输入签名密码。")
            passphrase = getpass.getpass("请输入项目签名密码（加密私钥需原密码）：")
        if not passphrase:
            raise BuildError("strict 构建的签名密码不能为空。")
    output.mkdir(parents=True, exist_ok=True)
    work = SOURCES / "build" / ("work-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    work.mkdir(parents=True)
    runner = Runner(work)
    print("构建目录：" + str(work), flush=True)
    build_venv = work / "build-venv"
    runner.run([python, "-I", "-m", "venv", build_venv])
    build_python = build_venv / "Scripts" / "python.exe"
    if not build_python.is_file():
        raise BuildError("Python 虚拟环境未创建成功，日志：" + str(runner.log_path))
    runner.run([
        build_python, "-m", "pip", "install", "--no-input",
        "-r", ROOT / "requirements-dev.txt", "fonttools[woff]==4.64.0",
    ])
    stage = work / "payload"
    app = stage / "app"
    prepare_app(app)
    sign_app(app, build_python, runner, args.development_key, passphrase)
    prepare_frontend(work, app, runner, node, npm)
    prepare_runtime(base, stage / "runtime")
    wheels = stage / "wheels"
    wheels.mkdir()
    runner.run([
        build_python, "-m", "pip", "wheel", "--no-input",
        "-r", ROOT / "requirements.txt", "--wheel-dir", wheels,
    ])
    (app / "installer-build.json").write_text(json.dumps({
        "version": version, "development_build": args.development_key,
        "security_mode": "strict", "python_version": python_info["version"],
        "platform": "windows-x64",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    version_file = work / "version.txt"
    version_file.write_text(version, encoding="utf-8")
    payload = work / "payload.zip"
    make_payload(stage, payload)
    prepare_fonts(work, build_python, runner)
    built_executable = work / "NotmyFault-Setup.exe"
    compile_installer(payload, version_file, built_executable, runner)
    executable = output / ("NotmyFault-" + version + "-windows-x64-setup.exe")
    staged_executable = output / (work.name + ".tmp")
    try:
        shutil.copyfile(built_executable, staged_executable)
        os.replace(staged_executable, executable)
    finally:
        staged_executable.unlink(missing_ok=True)
    print("安装器已生成：" + str(executable), flush=True)
    print("构建日志：" + str(runner.log_path), flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except (BuildError, OSError, subprocess.CalledProcessError, ValueError) as error:
        print("构建失败：" + str(error), file=sys.stderr)
        raise SystemExit(1)
