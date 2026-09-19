using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using NotmyFault.Setup;

public static class Smoke
{
    internal static string RegistryFixture;
    internal static string InstallerPath;
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern int RegOverridePredefKey(IntPtr key, IntPtr replacement);

    [STAThread]
    public static int Main(string[] args)
    {
        string probeAssembly = Environment.GetEnvironmentVariable("NOTMYFAULT_SMOKE_INSTALLER");
        if (!String.IsNullOrEmpty(probeAssembly))
            AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs request)
            {
                return new AssemblyName(request.Name).Name == AssemblyName.GetAssemblyName(probeAssembly).Name
                    ? Assembly.LoadFrom(probeAssembly) : null;
            };
        if (args.Length > 0 && Path.GetFileName(args[0]) == "dashboard.pyw")
        {
            string observation = Path.Combine(Environment.CurrentDirectory, "launch-observed.txt");
            File.WriteAllLines(observation + ".tmp", args);
            File.Move(observation + ".tmp", observation);
            return 0;
        }
        if (args.Length > 0 && (args[0] == "--uninstall-probe" || args[0] == "--uninstall-root"))
        {
            return Scenarios.ProbeUninstall(args);
        }
        string installer = Path.GetFullPath(args[0]);
        InstallerPath = installer;
        string workspace = Path.GetFullPath(args[1]);
        AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs request)
        {
            return new AssemblyName(request.Name).Name == AssemblyName.GetAssemblyName(installer).Name
                ? Assembly.LoadFrom(installer) : null;
        };
        bool interruptUpgrade = args.Length > 2 && args[2] == "--interrupt-upgrade";
        bool maintenanceOnly = args.Length > 2 && args[2] == "--maintenance-only";
        string registryPath = interruptUpgrade ? args[3] : @"Software\NotmyFaultInstallerTests\" + Guid.NewGuid().ToString("N");
        RegistryFixture = registryPath;
        IntPtr currentUser = new IntPtr(unchecked((int)0x80000001));
        try
        {
            Directory.CreateDirectory(workspace);
            string temporary = Path.Combine(workspace, "temp");
            Directory.CreateDirectory(temporary);
            Environment.SetEnvironmentVariable("TEMP", temporary);
            Environment.SetEnvironmentVariable("TMP", temporary);
            using (RegistryKey isolated = Registry.CurrentUser.CreateSubKey(registryPath))
            {
                int result = RegOverridePredefKey(currentUser, isolated.Handle.DangerousGetHandle());
                if (result != 0) throw new System.ComponentModel.Win32Exception(result);
                try
                {
                    if (interruptUpgrade) Scenarios.InterruptUpgrade(workspace, args[4]);
                    else if (maintenanceOnly) Scenarios.RunMaintenance(workspace);
                    else Scenarios.Run(installer, workspace);
                }
                finally
                {
                    result = RegOverridePredefKey(currentUser, IntPtr.Zero);
                    if (result != 0) throw new System.ComponentModel.Win32Exception(result);
                }
            }
            Console.WriteLine(maintenanceOnly ?
                "PASS: installer maintenance checks" : "PASS: all installer lifecycle checks");
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            return 1;
        }
        finally { Registry.CurrentUser.DeleteSubKeyTree(registryPath, false); }
    }
}

internal static class Scenarios
{
    private const string Registration = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\NotmyFault";
    private const string Password = "Smoke-临时 密码-2026!";
    private const string RemovedProgram = "program-a.bin";
    private const string InterruptedProgram = "program-b.bin";
    private const string PrivateKey = "app/.private/signing_private_key.pem";
    private const string PublicKey = "app/.private/signing_public.pem";
    private const string PrivateNote = "app/.private/notes.txt";
    private const string UserPluginResource = "app/user_plugins/sample/resource.txt";
    private static string workspace;

    private sealed class Progress : IProgress<InstallProgress>
    {
        public Action<InstallProgress> Observe;
        private string previous;
        public void Report(InstallProgress value)
        {
            if (value.Message != previous)
            {
                previous = value.Message;
                Console.WriteLine("  " + value.Message);
            }
            if (Observe != null) Observe(value);
        }
    }

    private static void Check(bool success, string message)
    {
        if (!success) throw new Exception(message);
    }

    private static void CheckWindows()
    {
        if (Application.Current == null) new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        Window[] windows = { new InstallerWindow(true), new UninstallerWindow(InstallEngine.DefaultDirectory, true) };
        foreach (Window window in windows)
        {
            try
            {
                foreach (string name in new[] { "PlainButton", "PrimaryButton", "PasswordField", "TextField" })
                {
                    Style style = window.Resources[name] as Style;
                    Check(style != null, "setup style is missing: " + name);
                    Control control = (Control)Activator.CreateInstance(style.TargetType);
                    control.Style = style;
                    control.ApplyTemplate();
                    control.Measure(new Size(400, 80));
                    control.Arrange(new Rect(0, 0, 400, 80));
                }
                FrameworkElement content = (FrameworkElement)window.Content;
                content.Measure(new Size(window.Width, window.Height));
                content.Arrange(new Rect(0, 0, window.Width, window.Height));
            }
            finally { window.Close(); }
        }
        Console.WriteLine("PASS: setup windows and XAML control templates initialize");
    }

    private static void CheckShortcuts(string root)
    {
        string directory = Path.Combine(root, "shortcut-install");
        string foreign = Path.Combine(root, "another-install");
        string log = Path.Combine(root, "shortcut.log");
        string ownedLink = InstallEngine.CreateShortcut(Path.Combine(root, "desktop"), directory, log);
        string foreignLink = InstallEngine.CreateShortcut(Path.Combine(root, "programs"), foreign, log);
        Check(File.Exists(ownedLink) && InstallMaintenance.ShortcutBelongsTo(ownedLink, directory), "created shortcut has incorrect ownership");
        Check(!InstallMaintenance.ShortcutBelongsTo(foreignLink, directory), "another installation's shortcut was accepted");
        InstallMaintenance.RemoveEntries(directory, new[] { ownedLink, foreignLink });
        Check(!File.Exists(ownedLink) && File.Exists(foreignLink), "shortcut cleanup removed the wrong installation's entry");
        InstallMaintenance.RemoveEntries(foreign, new[] { foreignLink });
        Check(!File.Exists(foreignLink), "shortcut cleanup left an owned entry");
        Console.WriteLine("PASS: shortcut creation and cleanup respect installation ownership");
    }

    private static void CheckLaunch(string root)
    {
        string directory = Path.Combine(root, "launch-install");
        string scripts = Path.Combine(directory, ".venv", "Scripts");
        string app = Path.Combine(directory, "app");
        Directory.CreateDirectory(scripts);
        Directory.CreateDirectory(app);
        File.Copy(Assembly.GetExecutingAssembly().Location, Path.Combine(scripts, "pythonw.exe"));
        string dashboard = Path.Combine(app, "dashboard.pyw");
        File.WriteAllText(dashboard, "");
        string observation = Path.Combine(app, "launch-observed.txt");
        string previous = Environment.GetEnvironmentVariable("NOTMYFAULT_SMOKE_INSTALLER");
        try
        {
            Environment.SetEnvironmentVariable("NOTMYFAULT_SMOKE_INSTALLER", Smoke.InstallerPath);
            foreach (bool upgraded in new[] { false, true })
            {
                InstallEngine.Launch(new InstallResult { Directory = directory, Upgraded = upgraded });
                Check(SpinWait.SpinUntil(delegate { return File.Exists(observation); }, 10000), "launcher did not start in the application directory");
                string[] arguments = File.ReadAllLines(observation);
                Check(arguments.Length == (upgraded ? 1 : 2) && arguments[0] == dashboard &&
                    (upgraded || arguments[1] == "--first-run"), "launcher passed incorrect first-run arguments");
                File.Delete(observation);
            }
        }
        finally { Environment.SetEnvironmentVariable("NOTMYFAULT_SMOKE_INSTALLER", previous); }
        Console.WriteLine("PASS: launcher selects first-run arguments for fresh installs");
    }

    internal static int ProbeUninstall(string[] args)
    {
        string directory = InstallMaintenance.PrepareUninstall(args[0] == "--uninstall-probe" ? new string[0] : args,
            Assembly.GetExecutingAssembly().Location);
        if (directory != null)
        {
            string observation = Path.Combine(directory, "handoff-observed.txt");
            File.WriteAllLines(observation + ".tmp", new[] { directory, Assembly.GetExecutingAssembly().Location });
            File.Move(observation + ".tmp", observation);
        }
        return 0;
    }

    private static void CheckUninstallHandoff(string root)
    {
        string directory = Path.Combine(root, "handoff-install");
        Directory.CreateDirectory(directory);
        File.WriteAllText(Path.Combine(directory, ".notmyfault-install"), "NotmyFault Windows installer 1\ntest\n");
        File.WriteAllText(Path.Combine(directory, ".notmyfault-files"), "NotmyFault-Uninstall.exe\n");
        string executable = Path.Combine(directory, "NotmyFault-Uninstall.exe");
        File.Copy(Assembly.GetExecutingAssembly().Location, executable);
        var start = new ProcessStartInfo(executable, "--uninstall-probe")
        {
            UseShellExecute = false, CreateNoWindow = true,
            WorkingDirectory = directory
        };
        start.EnvironmentVariables["NOTMYFAULT_SMOKE_INSTALLER"] = Smoke.InstallerPath;
        using (Process process = Process.Start(start))
        {
            if (!process.WaitForExit(10000)) { process.Kill(); throw new Exception("uninstall handoff did not release the parent process"); }
            Check(process.ExitCode == 0, "uninstall handoff parent failed");
        }
        string observation = Path.Combine(directory, "handoff-observed.txt");
        Check(SpinWait.SpinUntil(delegate { return File.Exists(observation); }, 15000), "uninstall handoff child did not resume");
        string[] resumed = File.ReadAllLines(observation);
        string relocated = resumed[1];
        Check(resumed[0] == directory && Path.GetDirectoryName(Path.GetDirectoryName(relocated)) == Path.GetTempPath().TrimEnd(Path.DirectorySeparatorChar),
            "uninstaller did not relocate into its temporary directory");
        Check(File.Exists(executable), "uninstall handoff changed its source executable");
        Check(SpinWait.SpinUntil(delegate { return !Directory.Exists(Path.GetDirectoryName(relocated)); }, 15000), "uninstaller left its temporary executable after exit");
        Console.WriteLine("PASS: uninstaller relocates, resumes after the parent exits, and removes its temporary copy");
    }

    private sealed class InterruptedUninstall : InstallMaintenance.UninstallOperations
    {
        internal string Failure;
        internal override IEnumerable<string> ShortcutPaths { get { return new string[0]; } }

        internal override void MoveDirectory(string source, string destination)
        {
            if (Failure == "move") throw new IOException("模拟目录移动失败");
            base.MoveDirectory(source, destination);
        }

        internal override void DeleteFile(string file)
        {
            if ((Failure == "delete" && Path.GetFileName(file) == InterruptedProgram) ||
                (Failure == "marker" && Path.GetFileName(file) == ".notmyfault-install"))
                throw new IOException("模拟文件删除失败");
            base.DeleteFile(file);
        }

        internal override void RemoveEntries(string directory)
        {
            if (Failure == "registration") throw new IOException("模拟注册信息清理失败");
            base.RemoveEntries(directory);
        }
    }

    internal static void RunMaintenance(string root)
    {
        Directory.CreateDirectory(root);
        CheckWindows();
        CheckShortcuts(root);
        CheckLaunch(root);
        CheckUninstallHandoff(root);
        CheckUpgradeRecovery(root);
        foreach (string failure in new[] { "move", "delete", "registration", "marker" })
        {
            string original = Path.Combine(root, failure);
            Directory.CreateDirectory(Path.Combine(original, "app", ".private"));
            Directory.CreateDirectory(Path.Combine(original, "app", "user_plugins", "sample"));
            File.WriteAllText(Path.Combine(original, ".notmyfault-install"), "NotmyFault Windows installer 1\ntest\n");
            string[] owned = { RemovedProgram, InterruptedProgram, PrivateKey, PublicKey, PrivateNote, UserPluginResource };
            File.WriteAllLines(Path.Combine(original, ".notmyfault-files"), owned);
            foreach (string relative in owned) File.WriteAllText(Path.Combine(original, relative), relative);
            File.WriteAllText(Path.Combine(original, "keep-me.txt"), "user data");
            using (RegistryKey registration = Registry.CurrentUser.CreateSubKey(Registration))
                registration.SetValue("InstallLocation", original);
            using (RegistryKey aumid = Registry.CurrentUser.CreateSubKey(@"Software\Classes\AppUserModelId\cuteaplane.notmyfault.app"))
            {
                aumid.SetValue("IconUri", "external-registration-icon");
                if (failure == "marker") aumid.SetValue("InstallLocation", Path.Combine(root, "another-installation"));
            }
            string retry = null;
            try
            {
                InstallMaintenance.UninstallAsync(original, new Progress(), CancellationToken.None,
                    new InterruptedUninstall { Failure = failure }).GetAwaiter().GetResult();
            }
            catch (UninstallFailure error) { retry = error.RetryDirectory; }
            Check(retry != null, "uninstall did not report the interruption");
            Check(Directory.Exists(retry), "retry directory does not exist");
            Check(File.Exists(Path.Combine(retry, ".notmyfault-uninstall")), "uninstall lost its cleanup list");
            Check(File.ReadAllText(Path.Combine(retry, "keep-me.txt")) == "user data", "interrupted uninstall changed user data");
            Check(File.ReadAllText(Path.Combine(retry, UserPluginResource)) == UserPluginResource, "interrupted uninstall changed a user plugin");
            Check(File.ReadAllText(Path.Combine(retry, PrivateNote)) == PrivateNote, "uninstaller removed another private file");
            if (failure == "move")
            {
                Check(String.Equals(original, retry, StringComparison.OrdinalIgnoreCase), "failed move changed the retry path");
                foreach (string relative in owned) Check(File.Exists(Path.Combine(original, relative)), "failed move deleted an installation file");
            }
            else
            {
                Check(!Directory.Exists(original), "uninstall did not move the installation before deleting files");
                Check(!File.Exists(Path.Combine(retry, RemovedProgram)), "uninstall did not reach file cleanup");
                if (failure == "delete") Check(File.Exists(Path.Combine(retry, InterruptedProgram)), "fixture did not interrupt partial cleanup");
                if (failure == "marker") Check(!File.Exists(Path.Combine(retry, ".notmyfault-files")), "fixture did not interrupt after marker cleanup");
            }
            bool refused = false;
            try { InstallEngine.ValidateDirectory(original); }
            catch (IOException) { refused = true; }
            Check(refused, "installation replaced a directory with unfinished uninstall cleanup");
            string retained = InstallMaintenance.UninstallAsync(retry, new Progress(), CancellationToken.None,
                new InterruptedUninstall()).GetAwaiter().GetResult();
            Check(Directory.Exists(retained) && !Directory.Exists(original), "retry did not retain user data separately");
            Check(File.ReadAllText(Path.Combine(retained, "keep-me.txt")) == "user data", "retry changed user data");
            Check(File.Exists(Path.Combine(retained, "app", "user_plugins", "sample", "resource.txt")), "retry removed a user plugin");
            Check(File.Exists(Path.Combine(retained, "app", ".private", "notes.txt")), "retry removed another private file");
            foreach (string relative in new[] { RemovedProgram, InterruptedProgram, PrivateKey, PublicKey,
                ".notmyfault-install", ".notmyfault-files", ".notmyfault-uninstall" })
                Check(!File.Exists(Path.Combine(retained, relative)), "retry left an installation file: " + relative);
            using (RegistryKey registration = Registry.CurrentUser.OpenSubKey(Registration))
                Check(registration == null, "retry left the installation registered");
            using (RegistryKey aumid = Registry.CurrentUser.OpenSubKey(@"Software\Classes\AppUserModelId\cuteaplane.notmyfault.app"))
                Check(failure == "marker" ? aumid != null : aumid == null, "uninstall used the icon path instead of AUMID ownership");
            Registry.CurrentUser.DeleteSubKeyTree(@"Software\Classes\AppUserModelId\cuteaplane.notmyfault.app", false);
            Check(InstallEngine.ValidateDirectory(original) == original, "completed uninstall did not release the original path");
            Console.WriteLine("PASS: uninstall resumes after " + failure + " failure and preserves user data");
        }
    }

    internal static void InterruptUpgrade(string directory, string stage)
    {
        var transaction = new InstallMaintenance.Transaction(directory, true);
        File.WriteAllText(Path.Combine(directory, "program.bin"), "new program");
        transaction.RestoreUserFiles();
        if (stage != "copied")
        {
            long size = InstallMaintenance.WriteState(directory, "new", transaction.Preserved);
            transaction.RegisterInstallation("new", size);
        }
        if (stage == "committed")
        {
            using (FileStream locked = new FileStream(Path.Combine(transaction.BackupPath, "program.bin"), FileMode.Open, FileAccess.Read, FileShare.None))
                Check(transaction.Commit().Length > 0, "backup cleanup did not leave a committed recovery record");
        }
        Environment.Exit(37);
    }

    private static void CheckUpgradeRecovery(string root)
    {
        foreach (string stage in new[] { "copied", "registered", "committed" })
        {
            string directory = Path.Combine(root, "upgrade-" + stage);
            Directory.CreateDirectory(directory);
            File.WriteAllText(Path.Combine(directory, "program.bin"), "old program");
            File.WriteAllText(Path.Combine(directory, "keep.txt"), "user data");
            long size = InstallMaintenance.WriteState(directory, "old", new HashSet<string> { "keep.txt" });
            InstallMaintenance.Register(directory, "old", size);
            using (RegistryKey registration = Registry.CurrentUser.OpenSubKey(Registration, true))
            {
                registration.SetValue("Extra", new[] { "first", "second" }, RegistryValueKind.MultiString);
                registration.SetValue("Raw", new byte[] { 0, 127, 255 }, RegistryValueKind.Binary);
            }
            var start = new ProcessStartInfo(Assembly.GetExecutingAssembly().Location,
                InstallEngine.Quote(Smoke.InstallerPath) + " " + InstallEngine.Quote(directory) +
                " --interrupt-upgrade " + InstallEngine.Quote(Smoke.RegistryFixture) + " " + stage)
                { UseShellExecute = false, CreateNoWindow = true };
            using (Process child = Process.Start(start))
            {
                Check(child.WaitForExit(30000), "upgrade fixture did not exit");
                Check(child.ExitCode == 37, "upgrade fixture did not interrupt the transaction");
            }
            Check(InstallEngine.IsInstalledDirectory(directory), "interrupted upgrade is absent from installation selection");
            Check(InstallEngine.ValidateDirectory(directory) == directory, "interrupted upgrade cannot be selected");
            Check(InstallEngine.GetInstalledVersion(directory) == (stage == "committed" ? "new" : "old"), "interrupted upgrade reports the wrong version");
            InstallMaintenance.RecoverUpgrade(directory);
            Check(File.ReadAllText(Path.Combine(directory, "program.bin")) == (stage == "committed" ? "new program" : "old program"), "recovery chose the wrong installation");
            Check(File.ReadAllText(Path.Combine(directory, "keep.txt")) == "user data", "upgrade recovery changed user data");
            Check(!File.Exists(InstallMaintenance.UpgradeRecordPath(directory)), "recovery left its record");
            using (RegistryKey registration = Registry.CurrentUser.OpenSubKey(Registration))
            {
                Check((string)registration.GetValue("DisplayVersion") == (stage == "committed" ? "new" : "old"), "recovery did not restore the registration");
                Check(((string[])registration.GetValue("Extra")).SequenceEqual(new[] { "first", "second" }), "recovery changed a registry multi-string");
                Check(((byte[])registration.GetValue("Raw")).SequenceEqual(new byte[] { 0, 127, 255 }), "recovery changed binary registry data");
            }
            Registry.CurrentUser.DeleteSubKeyTree(Registration, false);
            Console.WriteLine("PASS: upgrade recovery after process exit at " + stage);
        }
    }

    private static InstallResult Install(string directory, bool upgrade, string password, CancellationToken token, Progress progress)
    {
        using (var secret = new SecureString())
        {
            foreach (char character in password) secret.AppendChar(character);
            return new InstallEngine().InstallAsync(new InstallOptions
            {
                Directory = directory,
                SigningPassword = secret,
                Upgrade = upgrade,
                DesktopShortcut = false,
                StartMenuShortcut = false
            }, progress, token).GetAwaiter().GetResult();
        }
    }

    private static void RegistrationMatches(string directory)
    {
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(Registration))
        {
            Check(key != null, "installed application is absent from Programs");
            Check((string)key.GetValue("DisplayName") == "NotmyFault", "incorrect program name");
            Check((string)key.GetValue("DisplayVersion") == InstallEngine.PackageVersion, "incorrect registered version");
            Check(String.Equals((string)key.GetValue("InstallLocation"), directory, StringComparison.OrdinalIgnoreCase), "incorrect registered directory");
            string uninstall = (string)key.GetValue("UninstallString");
            Check(!String.IsNullOrEmpty(uninstall) && uninstall.Contains(directory), "missing uninstall command");
        }
        Check(String.Equals(InstallEngine.FindInstalledDirectory(), directory, StringComparison.OrdinalIgnoreCase), "installed directory discovery failed");
    }

    private static void RunPython(string directory, string arguments)
    {
        var start = new ProcessStartInfo(Path.Combine(directory, ".venv", "Scripts", "python.exe"), "-E -s -X utf8 -B " + arguments)
        {
            WorkingDirectory = Path.Combine(directory, "app"),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8
        };
        start.EnvironmentVariables.Remove("NOTMYFAULT_SIGNING_PASSPHRASE");
        start.EnvironmentVariables["APPDATA"] = Path.Combine(workspace, "profile");
        start.EnvironmentVariables["LOCALAPPDATA"] = Path.Combine(workspace, "profile", "local");
        using (Process process = Process.Start(start))
        {
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            if (!process.WaitForExit(120000))
            {
                process.Kill();
                throw new Exception("installed Python command timed out");
            }
            Console.Write(stdout.GetAwaiter().GetResult());
            string errors = stderr.GetAwaiter().GetResult();
            Check(process.ExitCode == 0, "installed Python failed: " + errors);
        }
    }

    private static void DataSurvives(string directory, byte[] key)
    {
        Check(File.ReadAllBytes(Path.Combine(directory, "app", ".private", "signing_private_key.pem")).SequenceEqual(key), "existing signing key changed");
        UserDataSurvives(directory);
    }

    private static void UserDataSurvives(string directory)
    {
        Check(File.ReadAllText(Path.Combine(directory, "app", "user_plugins", "sample", "resource.txt")) == "user plugin", "user plugin changed");
        Check(File.ReadAllText(Path.Combine(directory, "keep-me.txt")) == "unrelated file", "unknown file changed");
        Check(File.ReadAllText(Path.Combine(workspace, "profile", "NotmyFault", "config.json")) == "{\"smoke\":true}", "user configuration changed");
    }

    public static void Run(string installer, string root)
    {
        workspace = root;
        RunMaintenance(Path.Combine(root, "maintenance"));
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        string unrelated = Path.Combine(workspace, "unrelated");
        Directory.CreateDirectory(unrelated);
        File.WriteAllText(Path.Combine(unrelated, "keep.txt"), "keep");
        bool rejected = false;
        try { InstallEngine.ValidateDirectory(unrelated); }
        catch (IOException) { rejected = true; }
        Check(rejected, "non-product directory was accepted");
        Check(File.ReadAllText(Path.Combine(unrelated, "keep.txt")) == "keep", "directory validation changed unrelated data");
        Console.WriteLine("PASS: unrelated directory is rejected without modification");

        string cancelled = Path.Combine(workspace, "cancelled-install");
        using (var cancellation = new CancellationTokenSource())
        {
            var progress = new Progress { Observe = delegate(InstallProgress value)
            {
                if (value.Step == 1) cancellation.Cancel();
            } };
            bool stopped = false;
            try { Install(cancelled, false, Password, cancellation.Token, progress); }
            catch (OperationCanceledException) { stopped = true; }
            Check(stopped, "installation did not cancel");
        }
        Check(!Directory.Exists(cancelled), "cancelled installation left program files");
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(Registration))
            Check(key == null, "cancelled installation registered a program");
        Console.WriteLine("PASS: cancellation after Python setup removes the installation");

        string directory = Path.Combine(workspace, "安装 with spaces");
        InstallResult installed = Install(directory, false, Password, CancellationToken.None, new Progress());
        Check(!installed.Upgraded && File.Exists(installed.LauncherPath), "fresh install result is invalid");
        Check(InstallEngine.IsInstalledDirectory(directory), "fresh installation cannot be detected");
        RegistrationMatches(directory);
        RunPython(directory, "build.py verify");
        RunPython(directory, "-c \"import sys, pathlib, cryptography, fastapi, py7zr; assert pathlib.Path(sys.prefix).resolve() == pathlib.Path('..', '.venv').resolve(); print('installed runtime and dependencies work')\"");
        Check(!Directory.Exists(Path.Combine(directory, ".setup-data", "Temp")), "successful installation left temporary Python files");
        Console.WriteLine("PASS: offline installation, installed runtime, signatures and program registration");

        string busyMarker = Path.Combine(workspace, "running.txt");
        string stopMarker = Path.Combine(workspace, "stop.txt");
        string busyScript = Path.Combine(workspace, "running.py");
        File.WriteAllText(busyScript,
            "import pathlib,sys,time\npathlib.Path(sys.argv[1]).write_text('ready')\n" +
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.1)\n");
        using (Process running = Process.Start(new ProcessStartInfo(
            Path.Combine(directory, ".venv", "Scripts", "python.exe"),
            "-B \"" + busyScript + "\" \"" + busyMarker + "\" \"" + stopMarker + "\"")
            { UseShellExecute = false, CreateNoWindow = true }))
        {
            try
            {
                Stopwatch ready = Stopwatch.StartNew();
                while (!File.Exists(busyMarker) && !running.HasExited && ready.ElapsedMilliseconds < 10000) Thread.Sleep(50);
                Check(File.Exists(busyMarker), "running-process fixture did not start");
                bool upgradeRefused = false;
                try { Install(directory, true, Password, CancellationToken.None, new Progress()); }
                catch (IOException) { upgradeRefused = true; }
                Check(upgradeRefused, "upgrade changed a running installation");
                bool uninstallRefused = false;
                try { InstallMaintenance.UninstallAsync(directory, new Progress(), CancellationToken.None).GetAwaiter().GetResult(); }
                catch (IOException) { uninstallRefused = true; }
                Check(uninstallRefused, "uninstaller changed a running installation");
            }
            finally
            {
                File.WriteAllText(stopMarker, "stop");
                Check(running.WaitForExit(10000), "running-process fixture failed to exit");
            }
        }
        Console.WriteLine("PASS: upgrade and uninstall reject a running installation");

        byte[] signingKey = File.ReadAllBytes(Path.Combine(directory, "app", ".private", "signing_private_key.pem"));
        Directory.CreateDirectory(Path.Combine(directory, "app", "user_plugins", "sample"));
        File.WriteAllText(Path.Combine(directory, "app", "user_plugins", "sample", "resource.txt"), "user plugin");
        File.WriteAllText(Path.Combine(directory, "keep-me.txt"), "unrelated file");
        Directory.CreateDirectory(Path.Combine(workspace, "profile", "NotmyFault"));
        File.WriteAllText(Path.Combine(workspace, "profile", "NotmyFault", "config.json"), "{\"smoke\":true}");
        bool wrongPasswordRejected = false;
        try { Install(directory, true, "wrong password", CancellationToken.None, new Progress()); }
        catch (Exception error)
        {
            Check(!(error is OperationCanceledException), "wrong password unexpectedly cancelled");
            wrongPasswordRejected = true;
        }
        Check(wrongPasswordRejected, "upgrade accepted an incorrect signing password");
        DataSurvives(directory, signingKey);
        RegistrationMatches(directory);
        RunPython(directory, "build.py verify");
        Console.WriteLine("PASS: failed upgrade preserves the working installation, key and user data");

        using (var cancellation = new CancellationTokenSource())
        {
            var progress = new Progress { Observe = delegate(InstallProgress value)
            {
                if (value.Step == 1) cancellation.Cancel();
            } };
            bool stopped = false;
            try { Install(directory, true, Password, cancellation.Token, progress); }
            catch (OperationCanceledException) { stopped = true; }
            Check(stopped, "upgrade did not cancel");
        }
        DataSurvives(directory, signingKey);
        RegistrationMatches(directory);
        RunPython(directory, "build.py verify");
        Console.WriteLine("PASS: cancelled upgrade restores the previous installation");

        InstallResult upgraded = Install(directory, true, Password, CancellationToken.None, new Progress());
        Check(upgraded.Upgraded, "upgrade was reported as a fresh installation");
        DataSurvives(directory, signingKey);
        RegistrationMatches(directory);
        RunPython(directory, "build.py verify");
        RunPython(directory, "-c \"import sys,pathlib; assert pathlib.Path(sys.base_prefix).resolve() == pathlib.Path('..','runtime').resolve(); print('upgraded venv points to the installed runtime')\"");
        Console.WriteLine("PASS: upgrade preserves the signing key and data and replaces the runtime");

        File.Delete(Path.Combine(directory, ".notmyfault-install"));
        File.Delete(Path.Combine(directory, ".notmyfault-files"));
        File.Delete(Path.Combine(directory, "NotmyFault-Uninstall.exe"));
        Check(InstallEngine.IsInstalledDirectory(directory), "previous installer format is not recognized");
        Install(directory, true, Password, CancellationToken.None, new Progress());
        DataSurvives(directory, signingKey);
        RegistrationMatches(directory);
        RunPython(directory, "build.py verify");
        Console.WriteLine("PASS: installations from the previous installer can upgrade");

        string retained = InstallMaintenance.UninstallAsync(directory, new Progress(), CancellationToken.None).GetAwaiter().GetResult();
        Check(Directory.Exists(retained), "uninstaller did not report retained user data");
        UserDataSurvives(retained);
        Check(!File.Exists(Path.Combine(retained, "app", ".private", "signing_private_key.pem")), "uninstaller left the signing private key");
        Check(!File.Exists(Path.Combine(retained, "app", ".private", "signing_public.pem")), "uninstaller left the signing public key");
        Check(!Directory.Exists(directory), "uninstaller left the original installation directory");
        Check(!File.Exists(Path.Combine(retained, ".notmyfault-install")) && !File.Exists(Path.Combine(retained, ".notmyfault-files")), "retained data includes installation markers");
        Check(!File.Exists(Path.Combine(directory, "NotmyFault.vbs")), "uninstaller left the launcher");
        Check(!Directory.Exists(Path.Combine(directory, "runtime")), "uninstaller left the Python runtime");
        Check(!Directory.Exists(Path.Combine(directory, ".venv")), "uninstaller left the virtual environment");
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(Registration))
            Check(key == null, "uninstaller left the program registered");
        Check(!InstallEngine.IsInstalledDirectory(directory), "uninstalled program still detected as installed");
        Console.WriteLine("PASS: uninstall removes application files and registration while retaining user data");

        InstallResult reinstalled = Install(directory, false, Password, CancellationToken.None, new Progress());
        Check(!reinstalled.Upgraded, "reinstallation was reported as an upgrade");
        RegistrationMatches(directory);
        UserDataSurvives(retained);
        string remaining = InstallMaintenance.UninstallAsync(directory, new Progress(), CancellationToken.None).GetAwaiter().GetResult();
        Check(remaining == "" && !Directory.Exists(directory), "uninstall without retained files left the installation directory");
        Console.WriteLine("PASS: the original path accepts a fresh installation after uninstall");
    }
}
