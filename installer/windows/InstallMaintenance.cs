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

[assembly: System.Runtime.CompilerServices.InternalsVisibleTo("InstallerSmoke")]

namespace NotmyFault.Setup
{
    public sealed class UninstallFailure : IOException
    {
        public string RetryDirectory { get; private set; }

        internal UninstallFailure(string directory, Exception error)
            : base("卸载未完成，文件位于：" + directory + "\n解除文件占用或权限问题后可重试。" +
                "如已退出卸载窗口，可重新运行安装包并传入 --resume-uninstall " + InstallEngine.Quote(directory) +
                "。\n" + error.Message, error)
        {
            RetryDirectory = directory;
        }
    }

    public static class InstallMaintenance
    {
        internal const string RegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\NotmyFault";
        internal const string StateFile = ".notmyfault-install";
        internal const string FilesFile = ".notmyfault-files";
        internal const string UninstallFile = ".notmyfault-uninstall";
        private const string StateHeader = "NotmyFault Windows installer 1";
        private const string UninstallHeader = "NotmyFault Windows uninstall 1";

        internal static string UpgradeRecordPath(string directory)
        {
            return Path.Combine(Path.GetDirectoryName(directory), "." + Path.GetFileName(directory) + ".upgrade-state");
        }

        private static string PendingUpgradeDirectory(string directory)
        {
            string record = UpgradeRecordPath(directory);
            if (!File.Exists(record)) return directory;
            using (UpgradeRecord pending = UpgradeRecord.Read(directory, false))
                return !pending.Committed && Directory.Exists(pending.Backup) ? pending.Backup : directory;
        }

        internal static void RecoverUpgrade(string directory)
        {
            if (!File.Exists(UpgradeRecordPath(directory))) return;
            using (UpgradeRecord pending = UpgradeRecord.Read(directory, true))
            {
                EnsureNotRunning(directory);
                if (pending.Committed) DeleteTree(pending.Backup);
                else
                {
                    if (Directory.Exists(pending.Backup))
                    {
                        DeleteTree(directory);
                        Directory.Move(pending.Backup, directory);
                    }
                    pending.RestoreRegistration();
                }
            }
            File.Delete(UpgradeRecordPath(directory));
        }

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
                string path = PendingUpgradeDirectory(NormalizeDirectory(directory));
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
            directory = PendingUpgradeDirectory(NormalizeDirectory(directory));
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

        private enum InstalledFileKind { Program, UserData, SigningKey }

        private static InstalledFileKind ClassifyFile(string relative, bool installerOwned)
        {
            string value = relative.Replace('\\', '/');
            if (value.Equals("app/.private/signing_private_key.pem", StringComparison.OrdinalIgnoreCase) ||
                value.Equals("app/.private/signing_public.pem", StringComparison.OrdinalIgnoreCase))
                return InstalledFileKind.SigningKey;
            if (value.Equals("app/.private", StringComparison.OrdinalIgnoreCase) ||
                value.StartsWith("app/.private/", StringComparison.OrdinalIgnoreCase) ||
                value.Equals("app/user_plugins", StringComparison.OrdinalIgnoreCase) ||
                value.StartsWith("app/user_plugins/", StringComparison.OrdinalIgnoreCase) ||
                value.Equals("user_plugins", StringComparison.OrdinalIgnoreCase) ||
                value.StartsWith("user_plugins/", StringComparison.OrdinalIgnoreCase))
                return InstalledFileKind.UserData;
            return installerOwned ? InstalledFileKind.Program : InstalledFileKind.UserData;
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
            return EnumerateOrdinaryFiles(directory);
        }

        private static IEnumerable<string> EnumerateOrdinaryFiles(string directory)
        {
            foreach (string entry in Directory.EnumerateFileSystemEntries(directory))
            {
                FileAttributes attributes = File.GetAttributes(entry);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("安装文件不能位于文件或目录链接中：" + entry);
                if ((attributes & FileAttributes.Directory) != 0)
                {
                    foreach (string file in EnumerateOrdinaryFiles(entry)) yield return file;
                }
                else yield return entry;
            }
        }

        private static string Relative(string directory, string path)
        {
            string prefix = Path.GetFullPath(directory).TrimEnd('\\', '/') + Path.DirectorySeparatorChar;
            string full = Path.GetFullPath(path);
            if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                throw new IOException("文件路径超出安装目录。");
            return full.Substring(prefix.Length);
        }

        private static string OwnedPath(string directory, string relative, bool checkFilesystem = true)
        {
            if (String.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative))
                throw new InvalidDataException("安装文件清单包含无效路径。");
            foreach (string part in relative.Replace('\\', '/').Split('/'))
                if (String.IsNullOrEmpty(part) || part == "." || part == ".." || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                    throw new InvalidDataException("安装文件清单包含无效路径。");
            string path = Path.GetFullPath(Path.Combine(directory, relative));
            Relative(directory, path);
            if (checkFilesystem) EnsureOrdinaryPath(path);
            return path;
        }

        private static Dictionary<string, InstalledFileKind> ReadInstallationFiles(string directory)
        {
            List<string> files = new List<string>(EnumerateFiles(directory));
            HashSet<string> owned = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            string manifest = Path.Combine(directory, FilesFile);
            if (File.Exists(manifest))
            {
                foreach (string relative in File.ReadAllLines(manifest, Encoding.UTF8))
                {
                    OwnedPath(directory, relative, false);
                    owned.Add(relative.Replace('/', Path.DirectorySeparatorChar));
                }
            }
            else
                foreach (string file in files)
                {
                    string relative = Relative(directory, file);
                    if (LegacyProgramFile(relative)) owned.Add(relative);
                }
            foreach (string file in files)
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
            Dictionary<string, InstalledFileKind> classified = new Dictionary<string, InstalledFileKind>(StringComparer.OrdinalIgnoreCase);
            foreach (string file in files)
            {
                string relative = Relative(directory, file);
                classified.Add(relative, ClassifyFile(relative, owned.Contains(relative)));
            }
            return classified;
        }

        internal static long WriteState(string directory, string version, HashSet<string> preserved)
        {
            EnsureOrdinaryPath(directory);
            File.WriteAllText(Path.Combine(directory, StateFile), StateHeader + "\n" + version + "\n", new UTF8Encoding(false));
            List<string> owned = new List<string>();
            long size = 0;
            foreach (string file in EnumerateFiles(directory))
            {
                if (!SamePath(file, Path.Combine(directory, FilesFile))) size += new FileInfo(file).Length;
                string relative = Relative(directory, file);
                if (ClassifyFile(relative, true) == InstalledFileKind.Program && !preserved.Contains(relative) && relative != FilesFile) owned.Add(relative);
            }
            owned.Add(FilesFile);
            owned.Sort(StringComparer.OrdinalIgnoreCase);
            File.WriteAllLines(Path.Combine(directory, FilesFile), owned.ToArray(), new UTF8Encoding(false));
            return size + new FileInfo(Path.Combine(directory, FilesFile)).Length;
        }

        internal static void Register(string directory, string version, long size)
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

        private static IEnumerable<string> DefaultShortcutPaths()
        {
            foreach (Environment.SpecialFolder folder in new[] { Environment.SpecialFolder.DesktopDirectory, Environment.SpecialFolder.Programs })
                yield return Path.Combine(Environment.GetFolderPath(folder), "NotmyFault.lnk");
        }

        internal static void RemoveEntries(string directory, IEnumerable<string> shortcutPaths)
        {
            foreach (string shortcut in shortcutPaths)
            {
                if (ShortcutBelongsTo(shortcut, directory)) File.Delete(shortcut);
            }
            using (RegistryKey run = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
                if (run != null && CommandUsesDirectory(run.GetValue("NotmyFaultEngine") as string, directory)) run.DeleteValue("NotmyFaultEngine", false);
            bool ownProtocol;
            using (RegistryKey command = Registry.CurrentUser.OpenSubKey(@"Software\Classes\notmyfault\shell\open\command"))
                ownProtocol = command != null && CommandUsesDirectory(command.GetValue("") as string, directory);
            if (ownProtocol) Registry.CurrentUser.DeleteSubKeyTree(@"Software\Classes\notmyfault", false);
            bool ownRegistration;
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
                ownRegistration = key != null && SamePath(key.GetValue("InstallLocation") as string, directory);
            bool ownAumid = false;
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(@"Software\Classes\AppUserModelId\cuteaplane.notmyfault.app"))
            {
                if (key != null)
                {
                    string owner = key.GetValue("InstallLocation") as string;
                    ownAumid = String.IsNullOrEmpty(owner) ? ownRegistration : SamePath(owner, directory);
                }
            }
            if (ownAumid) Registry.CurrentUser.DeleteSubKeyTree(@"Software\Classes\AppUserModelId\cuteaplane.notmyfault.app", false);
            if (ownRegistration) Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, false);
        }

        internal static bool ShortcutBelongsTo(string path, string directory)
        {
            if (!File.Exists(path)) return false;
            return WithShortcut(path, delegate(object shortcut)
            {
                string target = shortcut.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, shortcut, null) as string;
                return SamePath(target, Path.Combine(directory, ".venv", "Scripts", "pythonw.exe")) ||
                    SamePath(target, Path.Combine(directory, "NotmyFault.vbs"));
            });
        }

        internal static T WithShortcut<T>(string path, Func<object, T> use)
        {
            object shell = null;
            object shortcut = null;
            try
            {
                Type type = Type.GetTypeFromProgID("WScript.Shell", true);
                shell = Activator.CreateInstance(type);
                shortcut = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
                return use(shortcut);
            }
            finally
            {
                if (shortcut != null && Marshal.IsComObject(shortcut)) Marshal.FinalReleaseComObject(shortcut);
                if (shell != null && Marshal.IsComObject(shell)) Marshal.FinalReleaseComObject(shell);
            }
        }

        public static Task<string> UninstallAsync(string directory, IProgress<InstallProgress> progress, CancellationToken token)
        {
            return UninstallAsync(directory, progress, token, new UninstallOperations());
        }

        internal class UninstallOperations
        {
            internal virtual IEnumerable<string> ShortcutPaths { get { return DefaultShortcutPaths(); } }
            internal virtual void MoveDirectory(string source, string destination) { Directory.Move(source, destination); }
            internal virtual void DeleteFile(string file)
            {
                if (!File.Exists(file)) return;
                File.SetAttributes(file, FileAttributes.Normal);
                File.Delete(file);
            }
            internal virtual void RemoveEntries(string directory) { InstallMaintenance.RemoveEntries(directory, ShortcutPaths); }
        }

        private sealed class UninstallPlan
        {
            internal string OriginalDirectory;
            internal readonly List<string> Files = new List<string>();

            internal void Write(string directory)
            {
                List<string> lines = new List<string> { UninstallHeader, OriginalDirectory };
                lines.AddRange(Files);
                string destination = OwnedPath(directory, UninstallFile);
                string temporary = destination + ".tmp-" + Guid.NewGuid().ToString("N");
                try
                {
                    using (FileStream stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write))
                    using (StreamWriter writer = new StreamWriter(stream, new UTF8Encoding(false)))
                        foreach (string line in lines) writer.WriteLine(line);
                    File.Move(temporary, destination);
                }
                finally
                {
                    try { File.Delete(temporary); }
                    catch (IOException) { }
                    catch (UnauthorizedAccessException) { }
                }
            }
        }

        private static UninstallPlan ReadUninstallPlan(string directory)
        {
            string[] lines = File.ReadAllLines(OwnedPath(directory, UninstallFile), Encoding.UTF8);
            if (lines.Length < 2 || lines[0] != UninstallHeader) throw new InvalidDataException("卸载清理清单无效。");
            var plan = new UninstallPlan { OriginalDirectory = NormalizeDirectory(lines[1]) };
            if (!SamePath(directory, plan.OriginalDirectory))
            {
                string prefix = Path.GetFileName(plan.OriginalDirectory) + "-保留文件-";
                string name = Path.GetFileName(directory);
                Guid identifier;
                if (!SamePath(Path.GetDirectoryName(directory), Path.GetDirectoryName(plan.OriginalDirectory)) ||
                    !name.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) ||
                    !Guid.TryParseExact(name.Substring(prefix.Length), "N", out identifier))
                    throw new InvalidDataException("卸载清理目录与原安装路径不匹配。");
            }
            for (int i = 2; i < lines.Length; i++)
            {
                OwnedPath(directory, lines[i]);
                if (lines[i].Equals(UninstallFile, StringComparison.OrdinalIgnoreCase) ||
                    ClassifyFile(lines[i], true) == InstalledFileKind.UserData)
                    throw new InvalidDataException("卸载清理清单包含保留文件。");
                plan.Files.Add(lines[i]);
            }
            return plan;
        }

        internal static bool HasUninstallPlan(string directory)
        {
            return File.Exists(OwnedPath(directory, UninstallFile));
        }

        internal static string FindPendingUninstall(string directory)
        {
            if (Directory.Exists(directory) && HasUninstallPlan(directory)) return directory;
            string parent = Path.GetDirectoryName(directory);
            if (!Directory.Exists(parent)) return null;
            foreach (string candidate in Directory.EnumerateDirectories(parent, Path.GetFileName(directory) + "-保留文件-*"))
                if (HasUninstallPlan(candidate) && SamePath(ReadUninstallPlan(candidate).OriginalDirectory, directory)) return candidate;
            return null;
        }

        internal static Task<string> UninstallAsync(string directory, IProgress<InstallProgress> progress, CancellationToken token,
            UninstallOperations operations)
        {
            return Task.Run(delegate
            {
                token.ThrowIfCancellationRequested();
                string path = NormalizeDirectory(directory);
                EnsureNotRunning(path);
                UninstallPlan plan;
                if (HasUninstallPlan(path)) plan = ReadUninstallPlan(path);
                else
                {
                    if (!IsInstalledDirectory(path)) throw new IOException("这个目录不是 NotmyFault 安装目录。");
                    plan = new UninstallPlan { OriginalDirectory = path };
                    foreach (var file in ReadInstallationFiles(path))
                        if (file.Value != InstalledFileKind.UserData) plan.Files.Add(file.Key);
                    plan.Files.Sort(StringComparer.OrdinalIgnoreCase);
                    plan.Write(path);
                }
                token.ThrowIfCancellationRequested();
                try
                {
                    if (SamePath(path, plan.OriginalDirectory))
                    {
                        string retained = NormalizeDirectory(Path.Combine(Path.GetDirectoryName(path),
                            Path.GetFileName(path) + "-保留文件-" + Guid.NewGuid().ToString("N")));
                        operations.MoveDirectory(path, retained);
                        path = retained;
                    }
                    int completed = 0;
                    foreach (string relative in plan.Files)
                    {
                        token.ThrowIfCancellationRequested();
                        if (relative == StateFile || relative == FilesFile) continue;
                        operations.DeleteFile(OwnedPath(path, relative));
                        completed++;
                        if (progress != null && completed % 30 == 0)
                            progress.Report(new InstallProgress { Step = 0, Fraction = (double)completed / plan.Files.Count, Message = "正在移除程序文件", Detail = relative });
                    }
                    token.ThrowIfCancellationRequested();
                    operations.RemoveEntries(plan.OriginalDirectory);
                    operations.DeleteFile(OwnedPath(path, FilesFile));
                    operations.DeleteFile(OwnedPath(path, StateFile));
                    DeleteEmptyDirectories(path);
                    operations.DeleteFile(OwnedPath(path, UninstallFile));
                }
                catch (Exception error)
                {
                    throw new UninstallFailure(path, error);
                }
                using (IEnumerator<string> entries = Directory.EnumerateFileSystemEntries(path).GetEnumerator())
                    if (!entries.MoveNext())
                    {
                        try { Directory.Delete(path); }
                        catch (IOException) { }
                        catch (UnauthorizedAccessException) { }
                    }
                if (progress != null) progress.Report(new InstallProgress { Step = 0, Fraction = 1, Message = "卸载已完成", Detail = "签名密钥已删除，用户配置、规则和插件已保留。" });
                return Directory.Exists(path) ? path : "";
            });
        }

        public static string PrepareUninstall(string[] args)
        {
            return PrepareUninstall(args, Assembly.GetExecutingAssembly().Location);
        }

        internal static string PrepareUninstall(string[] args, string executable)
        {
            string directory = Path.GetDirectoryName(executable);
            if (args.Length == 2 && args[0] == "--resume-uninstall")
            {
                directory = NormalizeDirectory(args[1]);
                ReadUninstallPlan(directory);
                return directory;
            }
            if (args.Length == 4 && args[0] == "--uninstall-root" && args[2] == "--wait-pid")
            {
                int pid;
                if (!Int32.TryParse(args[3], out pid)) throw new ArgumentException("卸载器启动参数无效。");
                try { using (Process parent = Process.GetProcessById(pid)) parent.WaitForExit(10000); }
                catch (ArgumentException) { }
                directory = NormalizeDirectory(args[1]);
                if (!IsInstalledDirectory(directory) && !HasUninstallPlan(directory)) throw new IOException("找不到有效的 NotmyFault 安装目录。");
                ScheduleTemporaryCleanup(executable);
                return directory;
            }
            if (args.Length != 0) throw new ArgumentException("卸载器启动参数无效。");
            if (!IsInstalledDirectory(directory) && !HasUninstallPlan(directory)) throw new IOException("请从 NotmyFault 安装目录或 Windows 已安装的应用中运行卸载。");
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

        private sealed class UpgradeRecord : IDisposable
        {
            internal string Backup;
            internal bool Committed;
            private bool hadRegistration;
            private FileStream stream;
            private readonly Dictionary<string, object> values = new Dictionary<string, object>();
            private readonly Dictionary<string, RegistryValueKind> kinds = new Dictionary<string, RegistryValueKind>();

            internal static UpgradeRecord Create(string directory, string backup, bool hadRegistration,
                Dictionary<string, object> values, Dictionary<string, RegistryValueKind> kinds)
            {
                string record = UpgradeRecordPath(directory);
                string temporary = record + "." + Guid.NewGuid().ToString("N") + ".tmp";
                try
                {
                    using (FileStream output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    using (BinaryWriter writer = new BinaryWriter(output, Encoding.UTF8, true))
                    {
                        writer.Write(false);
                        writer.Write("NotmyFault Windows upgrade 1");
                        writer.Write(directory);
                        writer.Write(backup);
                        writer.Write(hadRegistration);
                        writer.Write(values.Count);
                        foreach (var value in values)
                        {
                            writer.Write(value.Key);
                            RegistryValueKind kind = kinds[value.Key];
                            writer.Write((int)kind);
                            if (kind == RegistryValueKind.Binary || kind == RegistryValueKind.None)
                                writer.Write(Convert.ToBase64String((byte[])value.Value));
                            else if (kind == RegistryValueKind.MultiString)
                            {
                                string[] strings = (string[])value.Value;
                                writer.Write(strings.Length);
                                foreach (string item in strings) writer.Write(item);
                            }
                            else writer.Write(Convert.ToString(value.Value, System.Globalization.CultureInfo.InvariantCulture));
                        }
                        writer.Flush();
                        output.Flush(true);
                    }
                    File.Move(temporary, record);
                    return Read(directory, true);
                }
                finally { if (File.Exists(temporary)) File.Delete(temporary); }
            }

            internal static UpgradeRecord Read(string directory, bool writable)
            {
                string path = UpgradeRecordPath(directory);
                EnsureOrdinaryPath(path);
                var record = new UpgradeRecord();
                record.stream = new FileStream(path, FileMode.Open, writable ? FileAccess.ReadWrite : FileAccess.Read,
                    writable ? FileShare.Read : FileShare.ReadWrite);
                try
                {
                    using (BinaryReader reader = new BinaryReader(record.stream, Encoding.UTF8, true))
                    {
                        record.Committed = reader.ReadBoolean();
                        if (reader.ReadString() != "NotmyFault Windows upgrade 1" || !SamePath(reader.ReadString(), directory))
                            throw new InvalidDataException("升级恢复记录与安装目录不符。");
                        record.Backup = NormalizeDirectory(reader.ReadString());
                        string prefix = "." + Path.GetFileName(directory) + ".upgrade-";
                        string name = Path.GetFileName(record.Backup);
                        Guid suffix;
                        if (!SamePath(Path.GetDirectoryName(record.Backup), Path.GetDirectoryName(directory)) ||
                            !name.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) ||
                            !Guid.TryParseExact(name.Substring(prefix.Length), "N", out suffix))
                            throw new InvalidDataException("升级备份必须位于安装目录旁。");
                        record.hadRegistration = reader.ReadBoolean();
                        int count = reader.ReadInt32();
                        for (int i = 0; i < count; i++)
                        {
                            string key = reader.ReadString();
                            RegistryValueKind kind = (RegistryValueKind)reader.ReadInt32();
                            object decoded;
                            if (kind == RegistryValueKind.MultiString)
                            {
                                string[] strings = new string[reader.ReadInt32()];
                                for (int j = 0; j < strings.Length; j++) strings[j] = reader.ReadString();
                                decoded = strings;
                            }
                            else
                            {
                                string value = reader.ReadString();
                                decoded = value;
                                if (kind == RegistryValueKind.Binary || kind == RegistryValueKind.None) decoded = Convert.FromBase64String(value);
                                else if (kind == RegistryValueKind.DWord) decoded = Int32.Parse(value, System.Globalization.CultureInfo.InvariantCulture);
                                else if (kind == RegistryValueKind.QWord) decoded = Int64.Parse(value, System.Globalization.CultureInfo.InvariantCulture);
                            }
                            record.values.Add(key, decoded);
                            record.kinds.Add(key, kind);
                        }
                    }
                    return record;
                }
                catch { record.Dispose(); throw; }
            }

            internal void MarkCommitted()
            {
                stream.Position = 0;
                stream.WriteByte(1);
                stream.Flush(true);
                Committed = true;
            }

            internal void RestoreRegistration()
            {
                Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, false);
                if (hadRegistration)
                    using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath))
                        foreach (var value in values) key.SetValue(value.Key, value.Value, kinds[value.Key]);
            }

            public void Dispose() { stream.Dispose(); }
        }

        internal sealed class Transaction : IDisposable
        {
            internal readonly string DirectoryPath;
            internal readonly string BackupPath;
            internal readonly HashSet<string> Preserved = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            internal readonly List<string> NewShortcuts = new List<string>();
            internal string FailureLogPath;
            private readonly bool existed;
            private readonly Dictionary<string, object> oldRegistration = new Dictionary<string, object>();
            private readonly Dictionary<string, RegistryValueKind> oldKinds = new Dictionary<string, RegistryValueKind>();
            private readonly bool hadRegistration;
            private bool touchedRegistration;
            private bool committed;
            private UpgradeRecord recovery;

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
                    foreach (var file in ReadInstallationFiles(directory))
                        if (file.Value != InstalledFileKind.Program) Preserved.Add(file.Key);
                    BackupPath = Path.Combine(Path.GetDirectoryName(directory), "." + Path.GetFileName(directory) + ".upgrade-" + Guid.NewGuid().ToString("N"));
                    if (!SamePath(Path.GetDirectoryName(BackupPath), Path.GetDirectoryName(directory)))
                        throw new IOException("无法创建升级工作目录。");
                    recovery = UpgradeRecord.Create(directory, BackupPath, hadRegistration, oldRegistration, oldKinds);
                    try { Directory.Move(directory, BackupPath); }
                    catch
                    {
                        recovery.Dispose();
                        File.Delete(UpgradeRecordPath(directory));
                        throw;
                    }
                }
                try { Directory.CreateDirectory(directory); }
                catch
                {
                    try
                    {
                        if (BackupPath != null) Directory.Move(BackupPath, directory);
                    }
                    finally { if (recovery != null) recovery.Dispose(); }
                    if (recovery != null) File.Delete(UpgradeRecordPath(directory));
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

            internal void RegisterInstallation(string version, long size)
            {
                touchedRegistration = true;
                Register(DirectoryPath, version, size);
            }

            internal string Commit()
            {
                if (recovery != null) recovery.MarkCommitted();
                committed = true;
                try
                {
                    if (BackupPath != null) DeleteTree(BackupPath);
                    if (recovery != null)
                    {
                        recovery.Dispose();
                        recovery = null;
                        File.Delete(UpgradeRecordPath(DirectoryPath));
                    }
                    return "";
                }
                catch (IOException) { return "升级已完成，但旧程序文件未能全部清理。关闭占用文件的程序后可删除：" + BackupPath; }
                catch (UnauthorizedAccessException) { return "升级已完成，但旧程序文件未能全部清理。请检查目录权限后删除：" + BackupPath; }
            }

            public void Dispose()
            {
                if (committed)
                {
                    if (recovery != null) recovery.Dispose();
                    return;
                }
                try
                {
                    foreach (string path in NewShortcuts)
                        if (ShortcutBelongsTo(path, DirectoryPath)) File.Delete(path);
                    if (touchedRegistration)
                    {
                        Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, false);
                        if (hadRegistration)
                            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath))
                                foreach (KeyValuePair<string, object> value in oldRegistration) key.SetValue(value.Key, value.Value, oldKinds[value.Key]);
                    }
                    DeleteTree(DirectoryPath);
                    if (BackupPath != null) Directory.Move(BackupPath, DirectoryPath);
                    else if (existed) Directory.CreateDirectory(DirectoryPath);
                    if (recovery != null)
                    {
                        recovery.Dispose();
                        recovery = null;
                        File.Delete(UpgradeRecordPath(DirectoryPath));
                    }
                }
                catch (Exception ex)
                {
                    var error = new IOException(BackupPath == null ? "未能清理全部安装文件，请关闭占用文件的程序后删除：" + DirectoryPath :
                        "未能恢复原安装。恢复记录与原文件已保留，请关闭占用文件的程序后重新运行安装包并选择：" + DirectoryPath, ex);
                    if (!String.IsNullOrEmpty(FailureLogPath)) throw new InstallFailure(FailureLogPath, error);
                    throw error;
                }
                finally { if (recovery != null) recovery.Dispose(); }
            }
        }

        [DllImport("kernel32.dll", SetLastError = true)] private static extern IntPtr OpenProcess(uint access, bool inherit, int id);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder name, ref int size);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        [DllImport("shell32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr CommandLineToArgvW(string command, out int count);
        [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
    }
}
