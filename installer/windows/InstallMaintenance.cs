using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Win32;

namespace NotmyFault.Setup
{
    public static class InstallMaintenance
    {
        internal const string RegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\NotmyFault";
        internal const string StateFile = ".notmyfault-install";
        internal const string FilesFile = ".notmyfault-files";
        private const string StateHeader = "NotmyFault Windows installer 1";

        internal static string NormalizeDirectory(string directory)
        {
            if (String.IsNullOrWhiteSpace(directory)) throw new ArgumentException("请选择安装目录。");
            string path = Path.GetFullPath(directory.Trim());
            if (path.StartsWith(@"\\", StringComparison.Ordinal) || SamePath(path, Path.GetPathRoot(path)))
                throw new ArgumentException("请选择本机磁盘中的独立文件夹。");
            path = path.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            if (File.Exists(path)) throw new IOException("所选路径是文件，请选择文件夹。");
            EnsureOrdinaryPath(path);
            return path;
        }

        internal static void EnsureOrdinaryPath(string path)
        {
            string current = Path.GetFullPath(path);
            while (!String.IsNullOrEmpty(current))
            {
                if ((File.Exists(current) || Directory.Exists(current)) &&
                    (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("安装文件不能位于文件或目录链接中：" + current);
                current = Path.GetDirectoryName(current);
            }
        }

        public static bool IsInstalledDirectory(string directory)
        {
            try
            {
                string path = NormalizeDirectory(directory);
                string marker = Path.Combine(path, StateFile);
                if (File.Exists(marker))
                {
                    using (StreamReader reader = new StreamReader(marker, Encoding.UTF8))
                        return reader.ReadLine() == StateHeader && File.Exists(Path.Combine(path, FilesFile));
                }
                return File.Exists(Path.Combine(path, "app", "installer-build.json")) &&
                    File.Exists(Path.Combine(path, "app", "build.json")) &&
                    File.Exists(Path.Combine(path, "app", "build.json.sig")) &&
                    File.Exists(Path.Combine(path, "app", "build.py")) &&
                    File.Exists(Path.Combine(path, "app", "dashboard.pyw")) &&
                    File.Exists(Path.Combine(path, "runtime", "python.exe")) &&
                    File.Exists(Path.Combine(path, ".venv", "Scripts", "python.exe")) &&
                    File.Exists(Path.Combine(path, "NotmyFault.vbs"));
            }
            catch (IOException) { return false; }
            catch (ArgumentException) { return false; }
            catch (UnauthorizedAccessException) { return false; }
        }

        public static string GetInstalledVersion(string directory)
        {
            if (!IsInstalledDirectory(directory)) return "";
            string marker = Path.Combine(directory, StateFile);
            if (File.Exists(marker))
            {
                using (StreamReader reader = new StreamReader(marker, Encoding.UTF8))
                {
                    reader.ReadLine();
                    return reader.ReadLine() ?? "未知版本";
                }
            }
            string metadata = File.ReadAllText(Path.Combine(directory, "app", "installer-build.json"), Encoding.UTF8);
            var match = System.Text.RegularExpressions.Regex.Match(metadata, "\"version\"\\s*:\\s*\"([^\"]+)\"");
            return match.Success ? match.Groups[1].Value : "早期安装版本";
        }

        public static string FindInstalledDirectory()
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
            {
                string path = key == null ? null : key.GetValue("InstallLocation") as string;
                if (IsInstalledDirectory(path)) return NormalizeDirectory(path);
            }
            string fallback = InstallEngine.DefaultDirectory;
            return IsInstalledDirectory(fallback) ? fallback : null;
        }

        internal static bool SamePath(string first, string second)
        {
            if (String.IsNullOrWhiteSpace(first) || String.IsNullOrWhiteSpace(second)) return false;
            return String.Equals(Path.GetFullPath(first).TrimEnd('\\', '/'),
                Path.GetFullPath(second).TrimEnd('\\', '/'), StringComparison.OrdinalIgnoreCase);
        }

        internal static void EnsureNotRunning(string directory)
        {
            string prefix = NormalizeDirectory(directory) + Path.DirectorySeparatorChar;
            int currentId;
            using (Process current = Process.GetCurrentProcess()) currentId = current.Id;
            foreach (Process process in Process.GetProcesses())
            {
                using (process)
                {
                    if (process.Id == currentId) continue;
                    IntPtr handle = OpenProcess(0x1000, false, process.Id);
                    if (handle == IntPtr.Zero) continue;
                    try
                    {
                        StringBuilder name = new StringBuilder(32768);
                        int size = name.Capacity;
                        if (QueryFullProcessImageName(handle, 0, name, ref size) &&
                            name.ToString().StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                            throw new IOException("NotmyFault 仍在运行，请先关闭窗口并退出托盘中的引擎，然后重试。");
                    }
                    finally { CloseHandle(handle); }
                }
            }
        }

        private static bool IsPreserved(string relative)
        {
            string value = relative.Replace('\\', '/');
            return value.Equals("app/.private", StringComparison.OrdinalIgnoreCase) ||
                value.StartsWith("app/.private/", StringComparison.OrdinalIgnoreCase) ||
                value.Equals("app/user_plugins", StringComparison.OrdinalIgnoreCase) ||
                value.StartsWith("app/user_plugins/", StringComparison.OrdinalIgnoreCase) ||
                value.Equals("user_plugins", StringComparison.OrdinalIgnoreCase) ||
                value.StartsWith("user_plugins/", StringComparison.OrdinalIgnoreCase);
        }

        private static bool LegacyProgramFile(string relative)
        {
            string value = relative.Replace('\\', '/');
            foreach (string prefix in new[] { "runtime/", ".venv/", "wheels/", ".setup-data/", "app/notmyfault/", "app/dashboard/", "app/Win_toaster/" })
                if (value.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return true;
            foreach (string file in new[] { "NotmyFault.vbs", "install.log", "app/build.py", "app/nmf.py", "app/dashboard.pyw", "app/NOTMYFAULT.pyw",
                "app/requirements.txt", "app/build.json", "app/build.json.sig", "app/installer-build.json", "app/LICENSE", "app/logo.ico", "app/logo.png" })
                if (value.Equals(file, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        internal static IEnumerable<string> EnumerateFiles(string directory)
        {
            EnsureOrdinaryPath(directory);
            foreach (string entry in Directory.EnumerateFileSystemEntries(directory))
            {
                EnsureOrdinaryPath(entry);
                if (Directory.Exists(entry))
                {
                    foreach (string file in EnumerateFiles(entry)) yield return file;
                }
                else yield return entry;
            }
        }

        private static string Relative(string directory, string path)
        {
            string prefix = NormalizeDirectory(directory) + Path.DirectorySeparatorChar;
            string full = Path.GetFullPath(path);
            if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                throw new IOException("文件路径超出安装目录。");
            return full.Substring(prefix.Length);
        }

        private static string OwnedPath(string directory, string relative)
        {
            if (String.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative))
                throw new InvalidDataException("安装文件清单包含无效路径。");
            foreach (string part in relative.Replace('\\', '/').Split('/'))
                if (String.IsNullOrEmpty(part) || part == "." || part == ".." || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                    throw new InvalidDataException("安装文件清单包含无效路径。");
            string path = Path.GetFullPath(Path.Combine(directory, relative));
            Relative(directory, path);
            EnsureOrdinaryPath(path);
            return path;
        }

        internal static HashSet<string> ReadOwnedFiles(string directory)
        {
            HashSet<string> owned = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            string manifest = Path.Combine(directory, FilesFile);
            if (File.Exists(manifest))
            {
                foreach (string relative in File.ReadAllLines(manifest, Encoding.UTF8))
                {
                    OwnedPath(directory, relative);
                    if (!IsPreserved(relative)) owned.Add(relative);
                }
            }
            else
                foreach (string file in EnumerateFiles(directory))
                {
                    string relative = Relative(directory, file);
                    if (LegacyProgramFile(relative) && !IsPreserved(relative)) owned.Add(relative);
                }
            foreach (string file in EnumerateFiles(directory))
            {
                string relative = Relative(directory, file);
                if (!relative.EndsWith(".pyc", StringComparison.OrdinalIgnoreCase)) continue;
                string parent = Path.GetDirectoryName(relative);
                if (String.IsNullOrEmpty(parent) || Path.GetFileName(parent) != "__pycache__") continue;
                string filename = Path.GetFileName(relative);
                int tag = filename.IndexOf(".cpython-", StringComparison.Ordinal);
                if (tag < 0) continue;
                string source = Path.Combine(Path.GetDirectoryName(parent) ?? "", filename.Substring(0, tag) + ".py");
                if (owned.Contains(source)) owned.Add(relative);
            }
            owned.Add(StateFile);
            owned.Add(FilesFile);
            return owned;
        }

        internal static void WriteState(string directory, string version, HashSet<string> preserved)
        {
            File.WriteAllText(Path.Combine(directory, StateFile), StateHeader + "\n" + version + "\n", new UTF8Encoding(false));
            List<string> owned = new List<string>();
            foreach (string file in EnumerateFiles(directory))
            {
                string relative = Relative(directory, file);
                if (!IsPreserved(relative) && !preserved.Contains(relative) && relative != FilesFile) owned.Add(relative);
            }
            owned.Add(FilesFile);
            owned.Sort(StringComparer.OrdinalIgnoreCase);
            File.WriteAllLines(Path.Combine(directory, FilesFile), owned.ToArray(), new UTF8Encoding(false));
        }

        internal static void Register(string directory, string version)
        {
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath))
            {
                key.SetValue("DisplayName", "NotmyFault");
                key.SetValue("DisplayVersion", version);
                key.SetValue("Publisher", "NotmyFault Project");
                key.SetValue("InstallLocation", directory);
                key.SetValue("DisplayIcon", Path.Combine(directory, "app", "logo.ico"));
                key.SetValue("UninstallString", InstallEngine.Quote(Path.Combine(directory, "NotmyFault-Uninstall.exe")));
                key.SetValue("InstallDate", DateTime.Now.ToString("yyyyMMdd"));
                key.SetValue("NoModify", 1, RegistryValueKind.DWord);
                key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                long size = 0;
                foreach (string file in EnumerateFiles(directory)) size += new FileInfo(file).Length;
                key.SetValue("EstimatedSize", (int)Math.Min(Int32.MaxValue, (size + 1023) / 1024), RegistryValueKind.DWord);
            }
        }

        internal static void DeleteTree(string directory)
        {
            if (!Directory.Exists(directory)) return;
            EnsureOrdinaryPath(directory);
            foreach (string entry in Directory.EnumerateFileSystemEntries(directory))
            {
                FileAttributes attributes = File.GetAttributes(entry);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                {
                    if ((attributes & FileAttributes.Directory) != 0) Directory.Delete(entry);
                    else File.Delete(entry);
                }
                else if ((attributes & FileAttributes.Directory) != 0) DeleteTree(entry);
                else { File.SetAttributes(entry, FileAttributes.Normal); File.Delete(entry); }
            }
            Directory.Delete(directory);
        }

        private static void DeleteEmptyDirectories(string directory)
        {
            foreach (string child in Directory.GetDirectories(directory))
            {
                if ((File.GetAttributes(child) & FileAttributes.ReparsePoint) == 0) DeleteEmptyDirectories(child);
            }
            using (IEnumerator<string> entries = Directory.EnumerateFileSystemEntries(directory).GetEnumerator())
                if (!entries.MoveNext()) Directory.Delete(directory);
        }

        private static bool CommandUsesDirectory(string command, string directory)
        {
            if (String.IsNullOrEmpty(command)) return false;
            int count;
            IntPtr arguments = CommandLineToArgvW(command, out count);
            if (arguments == IntPtr.Zero) return false;
            try
            {
                if (count == 0) return false;
                string executable = Marshal.PtrToStringUni(Marshal.ReadIntPtr(arguments));
                return SamePath(executable, Path.Combine(directory, ".venv", "Scripts", "pythonw.exe")) ||
                    SamePath(executable, Path.Combine(directory, ".venv", "Scripts", "python.exe"));
            }
            finally { LocalFree(arguments); }
        }

        private static void RemoveEntries(string directory)
        {
            foreach (Environment.SpecialFolder folder in new[] { Environment.SpecialFolder.DesktopDirectory, Environment.SpecialFolder.Programs })
            {
                string shortcut = Path.Combine(Environment.GetFolderPath(folder), "NotmyFault.lnk");
                if (ShortcutBelongsTo(shortcut, directory)) File.Delete(shortcut);
            }
            using (RegistryKey run = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                if (run != null && CommandUsesDirectory(run.GetValue("NotmyFaultEngine") as string, directory)) run.DeleteValue("NotmyFaultEngine", false);
            bool ownProtocol;
            using (RegistryKey command = Registry.CurrentUser.OpenSubKey(@"Software\Classes\notmyfault\shell\open\command"))
                ownProtocol = command != null && CommandUsesDirectory(command.GetValue("") as string, directory);
            if (ownProtocol) Registry.CurrentUser.DeleteSubKeyTree(@"Software\Classes\notmyfault", false);
            bool ownAumid;
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(@"Software\Classes\AppUserModelId\cuteaplane.notmyfault.app"))
                ownAumid = key != null && SamePath(key.GetValue("IconUri") as string, Path.Combine(directory, "app", "logo.ico"));
            if (ownAumid) Registry.CurrentUser.DeleteSubKeyTree(@"Software\Classes\AppUserModelId\cuteaplane.notmyfault.app", false);
            bool ownRegistration;
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
                ownRegistration = key != null && SamePath(key.GetValue("InstallLocation") as string, directory);
            if (ownRegistration) Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, false);
        }

        internal static bool ShortcutBelongsTo(string path, string directory)
        {
            if (!File.Exists(path)) return false;
            object shell = null;
            object shortcut = null;
            try
            {
                Type type = Type.GetTypeFromProgID("WScript.Shell", true);
                shell = Activator.CreateInstance(type);
                shortcut = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
                string target = shortcut.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, shortcut, null) as string;
                return SamePath(target, Path.Combine(directory, ".venv", "Scripts", "pythonw.exe")) ||
                    SamePath(target, Path.Combine(directory, "NotmyFault.vbs"));
            }
            finally
            {
                if (shortcut != null && Marshal.IsComObject(shortcut)) Marshal.FinalReleaseComObject(shortcut);
                if (shell != null && Marshal.IsComObject(shell)) Marshal.FinalReleaseComObject(shell);
            }
        }

        public static Task<string> UninstallAsync(string directory, IProgress<InstallProgress> progress, CancellationToken token)
        {
            return Task.Run(delegate
            {
                token.ThrowIfCancellationRequested();
                string path = NormalizeDirectory(directory);
                if (!IsInstalledDirectory(path)) throw new IOException("这个目录不是 NotmyFault 安装目录。");
                EnsureNotRunning(path);
                HashSet<string> owned = ReadOwnedFiles(path);
                foreach (string file in EnumerateFiles(path)) { }
                token.ThrowIfCancellationRequested();
                int completed = 0;
                foreach (string relative in owned)
                {
                    if (relative == StateFile || relative == FilesFile) continue;
                    string file = OwnedPath(path, relative);
                    if (File.Exists(file)) { File.SetAttributes(file, FileAttributes.Normal); File.Delete(file); }
                    completed++;
                    if (progress != null && completed % 30 == 0)
                        progress.Report(new InstallProgress { Step = 0, Fraction = (double)completed / owned.Count, Message = "正在移除程序文件", Detail = relative });
                }
                RemoveEntries(path);
                File.Delete(Path.Combine(path, FilesFile));
                File.Delete(Path.Combine(path, StateFile));
                DeleteEmptyDirectories(path);
                if (progress != null) progress.Report(new InstallProgress { Step = 0, Fraction = 1, Message = "卸载已完成", Detail = "用户配置、规则、插件和签名密钥已保留。" });
                return Directory.Exists(path) ? path : "";
            });
        }

        public static string PrepareUninstall(string[] args)
        {
            string executable = Assembly.GetExecutingAssembly().Location;
            string directory = Path.GetDirectoryName(executable);
            if (args.Length == 4 && args[0] == "--uninstall-root" && args[2] == "--wait-pid")
            {
                int pid;
                if (!Int32.TryParse(args[3], out pid)) throw new ArgumentException("卸载器启动参数无效。");
                try { using (Process parent = Process.GetProcessById(pid)) parent.WaitForExit(10000); }
                catch (ArgumentException) { }
                directory = NormalizeDirectory(args[1]);
                if (!IsInstalledDirectory(directory)) throw new IOException("找不到有效的 NotmyFault 安装目录。");
                ScheduleTemporaryCleanup(executable);
                return directory;
            }
            if (args.Length != 0) throw new ArgumentException("卸载器启动参数无效。");
            if (!IsInstalledDirectory(directory)) throw new IOException("请从 NotmyFault 安装目录或 Windows 已安装的应用中运行卸载。");
            string temporary = Path.Combine(Path.GetTempPath(), "NotmyFault-Uninstall-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(temporary);
            string copy = Path.Combine(temporary, "NotmyFault-Uninstall.exe");
            File.Copy(executable, copy);
            using (Process child = Process.Start(new ProcessStartInfo
            {
                FileName = copy,
                Arguments = "--uninstall-root " + InstallEngine.Quote(directory) + " --wait-pid " + Process.GetCurrentProcess().Id,
                UseShellExecute = false,
                WorkingDirectory = temporary
            })) { }
            return null;
        }

        private static void ScheduleTemporaryCleanup(string executable)
        {
            string folder = Path.GetDirectoryName(executable);
            string name = Path.GetFileName(folder);
            Guid identifier;
            const string prefix = "NotmyFault-Uninstall-";
            if (!SamePath(Path.GetDirectoryName(folder), Path.GetTempPath()) || !name.StartsWith(prefix, StringComparison.Ordinal) ||
                !Guid.TryParseExact(name.Substring(prefix.Length), "N", out identifier) ||
                Path.GetFileName(executable) != "NotmyFault-Uninstall.exe") return;
            EnsureOrdinaryPath(executable);
            int processId;
            using (Process current = Process.GetCurrentProcess()) processId = current.Id;
            AppDomain.CurrentDomain.ProcessExit += delegate
            {
                string script = "Wait-Process -Id " + processId + " -ErrorAction SilentlyContinue\n" +
                    "Remove-Item -LiteralPath '" + executable.Replace("'", "''") + "' -Force -ErrorAction SilentlyContinue\n" +
                    "Remove-Item -LiteralPath '" + folder.Replace("'", "''") + "' -ErrorAction SilentlyContinue";
                try
                {
                    using (Process cleanup = Process.Start(new ProcessStartInfo
                    {
                        FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"),
                        Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + Convert.ToBase64String(Encoding.Unicode.GetBytes(script)),
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        WindowStyle = ProcessWindowStyle.Hidden,
                        WorkingDirectory = Path.GetTempPath()
                    })) { }
                }
                catch (Win32Exception) { }
            };
        }

        internal sealed class Transaction : IDisposable
        {
            internal readonly string DirectoryPath;
            internal readonly string BackupPath;
            internal readonly HashSet<string> Preserved = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            internal readonly List<string> NewShortcuts = new List<string>();
            private readonly bool existed;
            private readonly Dictionary<string, object> oldRegistration = new Dictionary<string, object>();
            private readonly Dictionary<string, RegistryValueKind> oldKinds = new Dictionary<string, RegistryValueKind>();
            private readonly bool hadRegistration;
            private bool touchedRegistration;
            private bool committed;

            internal Transaction(string directory, bool upgrade)
            {
                DirectoryPath = directory;
                existed = Directory.Exists(directory);
                using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
                {
                    hadRegistration = key != null;
                    if (key != null)
                    {
                        string installed = key.GetValue("InstallLocation") as string;
                        if (IsInstalledDirectory(installed) && !SamePath(installed, directory))
                            throw new IOException("已在其他目录安装 NotmyFault，请选择原安装目录进行升级：" + installed);
                        foreach (string name in key.GetValueNames())
                        {
                            oldRegistration[name] = key.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
                            oldKinds[name] = key.GetValueKind(name);
                        }
                    }
                }
                if (upgrade)
                {
                    EnsureNotRunning(directory);
                    HashSet<string> owned = ReadOwnedFiles(directory);
                    foreach (string file in EnumerateFiles(directory))
                    {
                        string relative = Relative(directory, file);
                        if (!owned.Contains(relative) || IsPreserved(relative)) Preserved.Add(relative);
                    }
                    BackupPath = Path.Combine(Path.GetDirectoryName(directory), "." + Path.GetFileName(directory) + ".upgrade-" + Guid.NewGuid().ToString("N"));
                    if (!SamePath(Path.GetDirectoryName(BackupPath), Path.GetDirectoryName(directory)))
                        throw new IOException("无法创建升级工作目录。");
                    Directory.Move(directory, BackupPath);
                }
                try { Directory.CreateDirectory(directory); }
                catch
                {
                    if (BackupPath != null) Directory.Move(BackupPath, directory);
                    throw;
                }
            }

            internal void RestoreUserFiles()
            {
                if (BackupPath == null) return;
                foreach (string relative in Preserved)
                {
                    string source = OwnedPath(BackupPath, relative);
                    string destination = OwnedPath(DirectoryPath, relative);
                    if (File.Exists(destination)) throw new IOException("新版程序与保留文件重名，请先移动这个文件：" + relative);
                    Directory.CreateDirectory(Path.GetDirectoryName(destination));
                    File.Copy(source, destination);
                }
            }

            internal void RegisterInstallation(string version)
            {
                touchedRegistration = true;
                Register(DirectoryPath, version);
            }

            internal string Commit()
            {
                committed = true;
                try
                {
                    if (BackupPath != null) DeleteTree(BackupPath);
                    return "";
                }
                catch (IOException) { return "升级已完成，但旧程序文件未能全部清理。关闭占用文件的程序后可删除：" + BackupPath; }
                catch (UnauthorizedAccessException) { return "升级已完成，但旧程序文件未能全部清理。请检查目录权限后删除：" + BackupPath; }
            }

            public void Dispose()
            {
                if (committed) return;
                foreach (string path in NewShortcuts)
                    if (ShortcutBelongsTo(path, DirectoryPath)) File.Delete(path);
                if (touchedRegistration)
                {
                    Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, false);
                    if (hadRegistration)
                        using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath))
                            foreach (KeyValuePair<string, object> value in oldRegistration) key.SetValue(value.Key, value.Value, oldKinds[value.Key]);
                }
                try
                {
                    DeleteTree(DirectoryPath);
                    if (BackupPath != null) Directory.Move(BackupPath, DirectoryPath);
                    else if (existed) Directory.CreateDirectory(DirectoryPath);
                }
                catch (Exception ex)
                {
                    throw new IOException(BackupPath == null ? "未能清理全部安装文件，请关闭占用文件的程序后删除：" + DirectoryPath :
                        "未能恢复原安装。原文件仍保存在：" + BackupPath + "。请关闭占用文件的程序，清理本次安装目录后将原文件移回：" + DirectoryPath, ex);
                }
            }
        }

        [DllImport("kernel32.dll", SetLastError = true)] private static extern IntPtr OpenProcess(uint access, bool inherit, int id);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder name, ref int size);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        [DllImport("shell32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr CommandLineToArgvW(string command, out int count);
        [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
    }
}
