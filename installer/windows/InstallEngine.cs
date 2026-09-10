using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace NotmyFault.Setup
{
    public sealed class InstallOptions
    {
        public string Directory;
        public SecureString SigningPassword;
        public bool DesktopShortcut = true;
        public bool StartMenuShortcut = true;
        public bool Upgrade;
    }

    public sealed class InstallProgress
    {
        public int Step;
        public double Fraction;
        public string Message;
        public string Detail;
    }

    public sealed class InstallResult
    {
        public string Directory;
        public string LauncherPath;
        public string LogPath;
        public bool Upgraded;
        public string Warning;
    }

    public sealed class InstallEngine
    {
        private const string PythonArguments = "-E -s -X utf8 -u ";

        public static string DefaultDirectory
        {
            get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "NotmyFault"); }
        }

        public static string PackageVersion
        {
            get
            {
                using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("NotmyFault.Version"))
                {
                    if (stream == null) return "开发版本";
                    using (StreamReader reader = new StreamReader(stream, Encoding.UTF8)) return reader.ReadToEnd().Trim();
                }
            }
        }

        public static string ValidateDirectory(string directory)
        {
            string path = InstallMaintenance.NormalizeDirectory(directory);
            if (System.IO.Directory.Exists(path) && !IsInstalledDirectory(path))
            {
                using (IEnumerator<string> entries = System.IO.Directory.EnumerateFileSystemEntries(path).GetEnumerator())
                    if (entries.MoveNext()) throw new IOException("请选择空文件夹，或已有 NotmyFault 的安装目录。");
            }
            return path;
        }

        public static bool IsInstalledDirectory(string directory) { return InstallMaintenance.IsInstalledDirectory(directory); }
        public static string FindInstalledDirectory() { return InstallMaintenance.FindInstalledDirectory(); }
        public static string GetInstalledVersion(string directory) { return InstallMaintenance.GetInstalledVersion(directory); }

        public Task<InstallResult> InstallAsync(InstallOptions options, IProgress<InstallProgress> progress, CancellationToken token)
        {
            if (options == null) throw new ArgumentNullException("options");
            if (options.SigningPassword == null || options.SigningPassword.Length == 0)
                throw new ArgumentException("请创建签名密码。");
            InstallOptions selected = new InstallOptions
            {
                Directory = options.Directory,
                SigningPassword = options.SigningPassword.Copy(),
                DesktopShortcut = options.DesktopShortcut,
                StartMenuShortcut = options.StartMenuShortcut,
                Upgrade = options.Upgrade
            };
            return Task.Run(delegate
            {
                try { return Install(selected, progress, token); }
                finally { selected.SigningPassword.Dispose(); }
            });
        }

        private InstallResult Install(InstallOptions options, IProgress<InstallProgress> progress, CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            string directory = ValidateDirectory(options.Directory);
            if (options.Upgrade != IsInstalledDirectory(directory))
                throw new IOException(options.Upgrade ? "原安装目录已变化，请重新选择。" : "该目录已安装 NotmyFault，请选择升级。");
            using (Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("NotmyFault.Payload.zip"))
            {
                if (payload == null) throw new InvalidOperationException("安装包缺少应用文件，请重新获取完整安装包。");
                using (InstallMaintenance.Transaction transaction = new InstallMaintenance.Transaction(directory, options.Upgrade))
                {
                string log = Path.Combine(directory, "install.log");
                using (FileStream initial = new FileStream(log, FileMode.CreateNew, FileAccess.Write, FileShare.Read)) { }
                try
                {
                    Log(log, "NotmyFault " + PackageVersion + " 安装开始");
                    string app = Path.Combine(directory, "app");
                    string runtime = Path.Combine(directory, "runtime", "python.exe");
                    string environment = Path.Combine(directory, ".venv");
                    string python = Path.Combine(environment, "Scripts", "python.exe");
                    string wheels = Path.Combine(directory, "wheels");
                    string requirements = Path.Combine(directory, ".setup-data", "requirements.txt");
                    using (ZipArchive archive = new ZipArchive(payload, ZipArchiveMode.Read, true))
                    {
                        Extract(archive, directory, false, progress, token);
                        ZipArchiveEntry requirementsEntry = archive.GetEntry("app/requirements.txt");
                        if (requirementsEntry == null) throw new InvalidDataException("安装包缺少依赖清单。");
                        System.IO.Directory.CreateDirectory(Path.GetDirectoryName(requirements));
                        using (Stream source = requirementsEntry.Open())
                        using (FileStream target = new FileStream(requirements, FileMode.CreateNew, FileAccess.Write))
                            source.CopyTo(target);
                        RequireFile(runtime);
                        if (!System.IO.Directory.Exists(wheels)) throw new InvalidDataException("安装包缺少离线依赖目录。");
                        Run(runtime, PythonArguments + "-m venv " + Quote(environment), directory, directory, log,
                            0, "正在创建 Python 虚拟环境", progress, token, false);
                        RequireFile(python);
                        Run(python, PythonArguments + "-m pip --isolated install --no-index --find-links " + Quote(wheels) +
                            " --only-binary=:all: --no-cache-dir --disable-pip-version-check --progress-bar off -r " + Quote(requirements),
                            directory, directory, log, 0, "正在安装 Python 离线依赖", progress, token, false);
                        Report(progress, 0, 1, "Python 环境配置完成", "");
                        Extract(archive, directory, true, progress, token);
                    }
                    RequireFile(runtime);
                    RequireFile(Path.Combine(app, "requirements.txt"));
                    RequireFile(Path.Combine(app, "build.py"));
                    RequireFile(Path.Combine(app, "dashboard.pyw"));
                    RequireFile(Path.Combine(app, "build.json"));
                    RequireFile(Path.Combine(app, "build.json.sig"));
                    if (System.IO.Directory.Exists(Path.Combine(app, ".private")))
                        throw new InvalidDataException("安装包不应包含私钥目录。");
                    Run(python, PythonArguments + Quote(Path.Combine(app, "build.py")) + " verify",
                        app, directory, log, 2, "正在检查随包插件签名", progress, token, false);
                    transaction.RestoreUserFiles();
                    if (options.Upgrade)
                    {
                        RequireFile(Path.Combine(app, ".private", "signing_private_key.pem"));
                        string unlock = "import pathlib,sys; " +
                            "from cryptography.hazmat.primitives.serialization import load_pem_private_key,Encoding,PublicFormat; " +
                            "key=load_pem_private_key(pathlib.Path('.private/signing_private_key.pem').read_bytes()," +
                            "password=sys.stdin.buffer.read()); " +
                            "pathlib.Path('.private/signing_public.pem').write_bytes(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw))";
                        try
                        {
                            Run(python, PythonArguments + "-c " + Quote(unlock), app, directory, log, 2,
                                "正在解锁原安装的签名密钥", progress, token, false, options.SigningPassword);
                        }
                        catch (InvalidOperationException ex)
                        {
                            throw new InvalidOperationException("无法解锁原安装的签名密钥，请检查原签名密码。", ex);
                        }
                    }
                    string build = "import os,runpy,sys; " +
                        "os.environ['NOTMYFAULT_SIGNING_PASSPHRASE']=sys.stdin.buffer.read().decode('utf-8'); " +
                        "sys.argv=['build.py','build','--security-mode=strict']; " +
                        "runpy.run_path('build.py',run_name='__main__')";
                    Run(python, PythonArguments + "-c " + Quote(build),
                        app, directory, log, 2, options.Upgrade ? "正在使用原签名密钥完成本地构建" : "正在创建签名密钥并完成本地构建",
                        progress, token, false, options.SigningPassword);
                    Run(python, PythonArguments + Quote(Path.Combine(app, "build.py")) + " verify",
                        app, directory, log, 2, "正在验证本地构建签名", progress, token, false);
                    Report(progress, 2, 1, "构建已完成", "");
                    RequireFile(Path.Combine(environment, "Scripts", "pythonw.exe"));

                    token.ThrowIfCancellationRequested();
                    Report(progress, 3, 0, "正在创建启动入口", "");
                    string launcher = WriteLauncher(directory);
                    using (Stream uninstaller = Assembly.GetExecutingAssembly().GetManifestResourceStream("NotmyFault.Uninstaller.exe"))
                    {
                        if (uninstaller == null) throw new InvalidDataException("安装包缺少卸载程序。");
                        using (FileStream target = File.Create(Path.Combine(directory, "NotmyFault-Uninstall.exe"))) uninstaller.CopyTo(target);
                    }
                    if (options.DesktopShortcut)
                    {
                        string shortcut = CreateShortcut(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), directory, log);
                        if (shortcut != null) transaction.NewShortcuts.Add(shortcut);
                    }
                    if (options.StartMenuShortcut)
                    {
                        string shortcut = CreateShortcut(Environment.GetFolderPath(Environment.SpecialFolder.Programs), directory, log);
                        if (shortcut != null) transaction.NewShortcuts.Add(shortcut);
                    }
                    token.ThrowIfCancellationRequested();
                    InstallMaintenance.DeleteTree(Path.Combine(directory, "wheels"));
                    InstallMaintenance.DeleteTree(Path.Combine(directory, ".setup-data"));
                    InstallMaintenance.WriteState(directory, PackageVersion, transaction.Preserved);
                    transaction.RegisterInstallation(PackageVersion);
                    Log(log, "安装完成");
                    string warning = transaction.Commit();
                    if (!String.IsNullOrEmpty(warning)) Log(log, warning);
                    Report(progress, 3, 1, "安装完成", directory);
                    return new InstallResult { Directory = directory, LauncherPath = launcher, LogPath = log, Upgraded = options.Upgrade, Warning = warning };
                }
                catch (OperationCanceledException)
                {
                    throw;
                }
                catch (Exception ex)
                {
                    Log(log, "安装失败：" + ex.Message);
                    string errorDirectory = Path.Combine(Path.GetTempPath(), "NotmyFault");
                    System.IO.Directory.CreateDirectory(errorDirectory);
                    string errorLog = Path.Combine(errorDirectory, "install-" + Guid.NewGuid().ToString("N") + ".log");
                    File.Copy(log, errorLog);
                    throw new InvalidOperationException(ex.Message + Environment.NewLine + "安装日志：" + errorLog, ex);
                }
                }
            }
        }

        private static void Extract(ZipArchive archive, string directory, bool application, IProgress<InstallProgress> progress, CancellationToken token)
        {
            int step = application ? 1 : 0;
            string message = application ? "正在释放程序文件" : "正在释放 Python 运行环境";
            Report(progress, step, 0, message, "");
            long total = 0;
            foreach (ZipArchiveEntry entry in archive.Entries)
                if (entry.FullName.Replace('\\', '/').StartsWith("app/", StringComparison.Ordinal) == application)
                    total = checked(total + entry.Length);
            long copied = 0;
            byte[] buffer = new byte[131072];
            Stopwatch clock = Stopwatch.StartNew();
            HashSet<string> files = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (ZipArchiveEntry entry in archive.Entries)
            {
                token.ThrowIfCancellationRequested();
                string relative = entry.FullName.Replace('\\', '/');
                string[] parts = relative.TrimEnd('/').Split('/');
                if (parts.Length == 0 || (parts[0] != "app" && parts[0] != "runtime" && parts[0] != "wheels"))
                    throw new InvalidDataException("安装包包含未知目录：" + relative);
                if ((parts[0] == "app") != application) continue;
                foreach (string part in parts)
                {
                    if (String.IsNullOrEmpty(part) || part == "." || part == ".." || part.EndsWith(".", StringComparison.Ordinal) ||
                        part.EndsWith(" ", StringComparison.Ordinal) || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                        throw new InvalidDataException("安装包包含无效路径：" + relative);
                }
                if (((entry.ExternalAttributes >> 16) & 0xF000) == 0xA000 || (entry.ExternalAttributes & 0x400) != 0)
                    throw new InvalidDataException("安装包包含目录或文件链接：" + relative);
                string destination = Path.GetFullPath(Path.Combine(directory, relative.Replace('/', Path.DirectorySeparatorChar)));
                if (!destination.StartsWith(directory + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("安装包路径超出安装目录：" + relative);
                if (relative.EndsWith("/", StringComparison.Ordinal))
                {
                    System.IO.Directory.CreateDirectory(destination);
                    continue;
                }
                if (!files.Add(destination)) throw new InvalidDataException("安装包包含重复文件：" + relative);
                System.IO.Directory.CreateDirectory(Path.GetDirectoryName(destination));
                using (Stream source = entry.Open())
                using (FileStream target = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    int count;
                    while ((count = source.Read(buffer, 0, buffer.Length)) != 0)
                    {
                        token.ThrowIfCancellationRequested();
                        target.Write(buffer, 0, count);
                        copied += count;
                        if (clock.ElapsedMilliseconds >= 100)
                        {
                            Report(progress, step, application && total > 0 ? Math.Min(0.99, (double)copied / total) : Double.NaN, message, relative);
                            clock.Restart();
                        }
                    }
                }
                File.SetLastWriteTimeUtc(destination, entry.LastWriteTime.UtcDateTime);
            }
            if (application) Report(progress, 1, 1, "程序文件已释放", "");
        }

        private static void Run(string executable, string arguments, string workingDirectory, string directory, string log,
            int step, string message, IProgress<InstallProgress> progress, CancellationToken token, bool completeStage = true,
            SecureString inputSecret = null)
        {
            token.ThrowIfCancellationRequested();
            Report(progress, step, Double.NaN, message, "");
            Log(log, message + Environment.NewLine + Quote(executable) + " " + arguments);
            using (FileStream readerStream = new FileStream(log, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            {
                readerStream.Seek(0, SeekOrigin.End);
                using (StreamReader reader = new StreamReader(readerStream, Encoding.UTF8))
                using (InstallProcess process = new InstallProcess(executable, arguments, workingDirectory, directory, log, inputSecret))
                {
                    try
                    {
                        while (!process.Wait(150))
                        {
                            token.ThrowIfCancellationRequested();
                            ReportOutput(reader, progress, step, message);
                        }
                        token.ThrowIfCancellationRequested();
                        uint exitCode = process.ExitCode;
                        process.StopChildren();
                        ReportOutput(reader, progress, step, message);
                        if (exitCode != 0) throw new InvalidOperationException(message + "失败，退出码 " + exitCode + "。");
                    }
                    catch
                    {
                        process.StopChildren();
                        process.Wait(5000);
                        ReportOutput(reader, progress, step, message);
                        throw;
                    }
                }
            }
            Log(log, message + "完成");
            if (completeStage) Report(progress, step, 1, message, "");
        }

        private static void ReportOutput(StreamReader reader, IProgress<InstallProgress> progress, int step, string message)
        {
            string output = reader.ReadToEnd().Trim();
            if (output.Length == 0) return;
            int line = output.LastIndexOf('\n');
            if (line >= 0) output = output.Substring(line + 1).Trim();
            if (output.Length > 300) output = output.Substring(0, 300);
            Report(progress, step, Double.NaN, message, output);
        }

        private static void Report(IProgress<InstallProgress> progress, int step, double fraction, string message, string detail)
        {
            if (progress != null) progress.Report(new InstallProgress { Step = step, Fraction = fraction, Message = message, Detail = detail });
        }

        private static void RequireFile(string path)
        {
            if (!File.Exists(path)) throw new FileNotFoundException("安装文件缺失：" + path, path);
        }

        private static void Log(string path, string message)
        {
            File.AppendAllText(path, "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "] " + message + Environment.NewLine, new UTF8Encoding(false));
        }

        internal static string Quote(string value)
        {
            StringBuilder result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char character in value)
            {
                if (character == '\\') { slashes++; continue; }
                if (character == '"') result.Append('\\', slashes * 2 + 1);
                else result.Append('\\', slashes);
                slashes = 0;
                result.Append(character);
            }
            result.Append('\\', slashes * 2);
            return result.Append('"').ToString();
        }

        private static string WriteLauncher(string directory)
        {
            string path = Path.Combine(directory, "NotmyFault.vbs");
            string text = "Option Explicit\r\nDim shell, files, root, q\r\n" +
                "Set shell = CreateObject(\"WScript.Shell\")\r\nSet files = CreateObject(\"Scripting.FileSystemObject\")\r\n" +
                "root = files.GetParentFolderName(WScript.ScriptFullName)\r\nq = Chr(34)\r\n" +
                "shell.CurrentDirectory = root & \"\\app\"\r\n" +
                "shell.Run q & root & \"\\.venv\\Scripts\\pythonw.exe\" & q & \" \" & q & root & \"\\app\\dashboard.pyw\" & q, 0, False\r\n";
            File.WriteAllText(path, text, Encoding.Unicode);
            return path;
        }

        private static string CreateShortcut(string folder, string directory, string log)
        {
            if (String.IsNullOrEmpty(folder)) throw new IOException("无法取得快捷方式目录。");
            System.IO.Directory.CreateDirectory(folder);
            string path = Path.Combine(folder, "NotmyFault.lnk");
            if (File.Exists(path))
            {
                Log(log, "已有同名快捷方式，保留：" + path);
                return null;
            }
            object shell = null;
            object shortcut = null;
            try
            {
                Type type = Type.GetTypeFromProgID("WScript.Shell", true);
                shell = Activator.CreateInstance(type);
                shortcut = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
                Type shortcutType = shortcut.GetType();
                shortcutType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut,
                    new object[] { Path.Combine(directory, ".venv", "Scripts", "pythonw.exe") });
                shortcutType.InvokeMember("Arguments", BindingFlags.SetProperty, null, shortcut,
                    new object[] { Quote(Path.Combine(directory, "app", "dashboard.pyw")) });
                shortcutType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut,
                    new object[] { Path.Combine(directory, "app") });
                shortcutType.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "NotmyFault" });
                shortcutType.InvokeMember("IconLocation", BindingFlags.SetProperty, null, shortcut,
                    new object[] { Path.Combine(directory, "app", "logo.ico") });
                shortcutType.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
                Log(log, "已创建快捷方式：" + path);
                return path;
            }
            finally
            {
                if (shortcut != null && Marshal.IsComObject(shortcut)) Marshal.FinalReleaseComObject(shortcut);
                if (shell != null && Marshal.IsComObject(shell)) Marshal.FinalReleaseComObject(shell);
            }
        }

        public static void Launch(InstallResult result)
        {
            if (result == null) throw new ArgumentNullException("result");
            string python = Path.Combine(result.Directory, ".venv", "Scripts", "pythonw.exe");
            string app = Path.Combine(result.Directory, "app");
            RequireFile(python);
            RequireFile(Path.Combine(app, "dashboard.pyw"));
            Process process = Process.Start(new ProcessStartInfo
            {
                FileName = python,
                Arguments = Quote(Path.Combine(app, "dashboard.pyw")) + (result.Upgraded ? "" : " --first-run"),
                WorkingDirectory = app,
                UseShellExecute = false,
                CreateNoWindow = true
            });
            if (process != null) process.Dispose();
        }

        private sealed class InstallProcess : IDisposable
        {
            private IntPtr job;
            private IntPtr process;

            public InstallProcess(string executable, string arguments, string workingDirectory, string directory, string log, SecureString inputSecret)
            {
                IntPtr output = IntPtr.Zero;
                IntPtr input = IntPtr.Zero;
                IntPtr inputWriter = IntPtr.Zero;
                IntPtr environment = IntPtr.Zero;
                ProcessInformation info = new ProcessInformation();
                try
                {
                    job = CreateJobObject(IntPtr.Zero, null);
                    Check(job != IntPtr.Zero);
                    JobExtendedLimitInformation limits = new JobExtendedLimitInformation();
                    limits.BasicLimitInformation.LimitFlags = 0x2000;
                    Check(SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(JobExtendedLimitInformation))));
                    SecurityAttributes attributes = new SecurityAttributes();
                    attributes.Length = Marshal.SizeOf(typeof(SecurityAttributes));
                    attributes.InheritHandle = true;
                    output = CreateFile(log, 0x00000004, 3, ref attributes, 4, 0x80, IntPtr.Zero);
                    Check(output != new IntPtr(-1));
                    if (inputSecret == null)
                    {
                        input = CreateFile("NUL", 0x80000000, 3, ref attributes, 3, 0x80, IntPtr.Zero);
                        Check(input != new IntPtr(-1));
                    }
                    else
                    {
                        Check(CreatePipe(out input, out inputWriter, ref attributes, 65536));
                        Check(SetHandleInformation(inputWriter, 1, 0));
                    }
                    StartupInfo start = new StartupInfo();
                    start.Size = Marshal.SizeOf(typeof(StartupInfo));
                    start.Flags = 0x100;
                    start.StandardInput = input;
                    start.StandardOutput = output;
                    start.StandardError = output;
                    environment = Marshal.StringToHGlobalUni(BuildEnvironment(directory));
                    // 挂起的 Python 先加入 Job，venv 和 pip 创建的子进程会继承同一个 Job。
                    Check(CreateProcess(executable, new StringBuilder(Quote(executable) + " " + arguments), IntPtr.Zero, IntPtr.Zero,
                        true, 0x08000404, environment, workingDirectory, ref start, out info));
                    process = info.Process;
                    Check(AssignProcessToJobObject(job, process));
                    Check(ResumeThread(info.Thread) != UInt32.MaxValue);
                    if (inputSecret != null) WriteSecret(inputWriter, inputSecret);
                }
                catch
                {
                    if (process != IntPtr.Zero) TerminateProcess(process, 1);
                    Dispose();
                    throw;
                }
                finally
                {
                    if (info.Thread != IntPtr.Zero) CloseHandle(info.Thread);
                    if (output != IntPtr.Zero && output != new IntPtr(-1)) CloseHandle(output);
                    if (input != IntPtr.Zero && input != new IntPtr(-1)) CloseHandle(input);
                    if (inputWriter != IntPtr.Zero) CloseHandle(inputWriter);
                    if (environment != IntPtr.Zero) Marshal.FreeHGlobal(environment);
                }
            }

            private static void WriteSecret(IntPtr pipe, SecureString secret)
            {
                IntPtr plain = IntPtr.Zero;
                char[] characters = new char[secret.Length];
                byte[] bytes = null;
                try
                {
                    plain = Marshal.SecureStringToGlobalAllocUnicode(secret);
                    Marshal.Copy(plain, characters, 0, characters.Length);
                    bytes = new UTF8Encoding(false, true).GetBytes(characters);
                    uint written;
                    Check(WriteFile(pipe, bytes, (uint)bytes.Length, out written, IntPtr.Zero));
                    if (written != bytes.Length) throw new IOException("无法将签名密码传给构建进程。");
                }
                finally
                {
                    Array.Clear(characters, 0, characters.Length);
                    if (bytes != null) Array.Clear(bytes, 0, bytes.Length);
                    if (plain != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(plain);
                }
            }

            public bool Wait(uint milliseconds)
            {
                uint status = WaitForSingleObject(process, milliseconds);
                if (status == UInt32.MaxValue) throw new Win32Exception(Marshal.GetLastWin32Error());
                return status == 0;
            }

            public uint ExitCode
            {
                get { uint code; Check(GetExitCodeProcess(process, out code)); return code; }
            }

            public void StopChildren()
            {
                if (job != IntPtr.Zero) Check(TerminateJobObject(job, 1));
            }

            public void Dispose()
            {
                if (job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; }
                if (process != IntPtr.Zero) { CloseHandle(process); process = IntPtr.Zero; }
            }

            private static string BuildEnvironment(string directory)
            {
                SortedDictionary<string, string> variables = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
                {
                    string key = (string)entry.Key;
                    if (key.StartsWith("PYTHON", StringComparison.OrdinalIgnoreCase) || key.StartsWith("PIP_", StringComparison.OrdinalIgnoreCase) ||
                        key.StartsWith("NOTMYFAULT", StringComparison.OrdinalIgnoreCase)) continue;
                    variables[key] = (string)entry.Value;
                }
                variables["PIP_CONFIG_FILE"] = "NUL";
                variables["PIP_NO_INDEX"] = "1";
                variables["PIP_DISABLE_PIP_VERSION_CHECK"] = "1";
                variables["APPDATA"] = Path.Combine(directory, ".setup-data", "Roaming");
                variables["LOCALAPPDATA"] = Path.Combine(directory, ".setup-data", "Local");
                string temporary = Path.Combine(directory, ".setup-data", "Temp");
                System.IO.Directory.CreateDirectory(temporary);
                variables["TEMP"] = temporary;
                variables["TMP"] = temporary;
                StringBuilder block = new StringBuilder();
                foreach (KeyValuePair<string, string> entry in variables) block.Append(entry.Key).Append('=').Append(entry.Value).Append('\0');
                return block.Append('\0').ToString();
            }

            private static void Check(bool success)
            {
                if (!success) throw new Win32Exception(Marshal.GetLastWin32Error());
            }

            [StructLayout(LayoutKind.Sequential)]
            private struct SecurityAttributes { public int Length; public IntPtr Descriptor; [MarshalAs(UnmanagedType.Bool)] public bool InheritHandle; }
            [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
            private struct StartupInfo
            {
                public int Size; public string Reserved; public string Desktop; public string Title;
                public uint X; public uint Y; public uint XSize; public uint YSize; public uint XCountChars; public uint YCountChars;
                public uint FillAttribute; public uint Flags; public short ShowWindow; public short ReservedSize;
                public IntPtr ReservedPointer; public IntPtr StandardInput; public IntPtr StandardOutput; public IntPtr StandardError;
            }
            [StructLayout(LayoutKind.Sequential)]
            private struct ProcessInformation { public IntPtr Process; public IntPtr Thread; public uint ProcessId; public uint ThreadId; }
            [StructLayout(LayoutKind.Sequential)]
            private struct JobBasicLimitInformation
            {
                public long ProcessTimeLimit; public long JobTimeLimit; public uint LimitFlags;
                public UIntPtr MinimumWorkingSet; public UIntPtr MaximumWorkingSet; public uint ActiveProcessLimit;
                public UIntPtr Affinity; public uint PriorityClass; public uint SchedulingClass;
            }
            [StructLayout(LayoutKind.Sequential)]
            private struct IoCounters
            {
                public ulong ReadOperations; public ulong WriteOperations; public ulong OtherOperations;
                public ulong ReadBytes; public ulong WriteBytes; public ulong OtherBytes;
            }
            [StructLayout(LayoutKind.Sequential)]
            private struct JobExtendedLimitInformation
            {
                public JobBasicLimitInformation BasicLimitInformation; public IoCounters IoInfo;
                public UIntPtr ProcessMemoryLimit; public UIntPtr JobMemoryLimit; public UIntPtr PeakProcessMemory; public UIntPtr PeakJobMemory;
            }
            [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool SetInformationJobObject(IntPtr job, int informationClass, ref JobExtendedLimitInformation information, uint length);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool CloseHandle(IntPtr handle);
            [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            private static extern IntPtr CreateFile(string name, uint access, uint share, ref SecurityAttributes attributes, uint creation, uint flags, IntPtr template);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool CreatePipe(out IntPtr read, out IntPtr write, ref SecurityAttributes attributes, uint size);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool WriteFile(IntPtr file, byte[] buffer, uint count, out uint written, IntPtr overlapped);
            [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            private static extern bool CreateProcess(string application, StringBuilder commandLine, IntPtr processAttributes, IntPtr threadAttributes,
                bool inheritHandles, uint flags, IntPtr environment, string directory, ref StartupInfo startupInfo, out ProcessInformation information);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern uint ResumeThread(IntPtr thread);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool TerminateProcess(IntPtr process, uint exitCode);
        }
    }
}
