using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security;
using System.Threading;
using Microsoft.Win32;
using NotmyFault.Setup;

public static class Smoke
{
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern int RegOverridePredefKey(IntPtr key, IntPtr replacement);

    public static int Main(string[] args)
    {
        string installer = Path.GetFullPath(args[0]);
        string workspace = Path.GetFullPath(args[1]);
        AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs request)
        {
            return new AssemblyName(request.Name).Name == AssemblyName.GetAssemblyName(installer).Name
                ? Assembly.LoadFrom(installer) : null;
        };
        string registryPath = @"Software\NotmyFaultInstallerTests\" + Guid.NewGuid().ToString("N");
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
                try { Scenarios.Run(installer, workspace); }
                finally
                {
                    result = RegOverridePredefKey(currentUser, IntPtr.Zero);
                    if (result != 0) throw new System.ComponentModel.Win32Exception(result);
                }
            }
            Console.WriteLine("PASS: all installer lifecycle checks");
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
        Check(File.ReadAllText(Path.Combine(directory, "app", "user_plugins", "sample", "resource.txt")) == "user plugin", "user plugin changed");
        Check(File.ReadAllText(Path.Combine(directory, "keep-me.txt")) == "unrelated file", "unknown file changed");
        Check(File.ReadAllText(Path.Combine(workspace, "profile", "NotmyFault", "config.json")) == "{\"smoke\":true}", "user configuration changed");
    }

    public static void Run(string installer, string root)
    {
        workspace = root;
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
        DataSurvives(directory, signingKey);
        Check(!File.Exists(Path.Combine(directory, "NotmyFault.vbs")), "uninstaller left the launcher");
        Check(!Directory.Exists(Path.Combine(directory, "runtime")), "uninstaller left the Python runtime");
        Check(!Directory.Exists(Path.Combine(directory, ".venv")), "uninstaller left the virtual environment");
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(Registration))
            Check(key == null, "uninstaller left the program registered");
        Check(!InstallEngine.IsInstalledDirectory(directory), "uninstalled program still detected as installed");
        Console.WriteLine("PASS: uninstall removes application files and registration while retaining user data");
    }
}
