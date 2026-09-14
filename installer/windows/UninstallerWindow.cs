using System;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace NotmyFault.Setup
{
    public static class UninstallProgram
    {
        [STAThread]
        public static int Main(string[] args)
        {
            try
            {
                bool preview = Array.IndexOf(args, "--preview") >= 0;
                string directory = preview ? InstallEngine.DefaultDirectory : InstallMaintenance.PrepareUninstall(args);
                if (directory == null) return 0;
                var app = new Application();
                app.DispatcherUnhandledException += delegate(object sender, System.Windows.Threading.DispatcherUnhandledExceptionEventArgs e)
                {
                    MessageBox.Show(e.Exception.Message, "NotmyFault 卸载", MessageBoxButton.OK, MessageBoxImage.Error);
                    e.Handled = true;
                };
                return app.Run(new UninstallerWindow(directory, preview));
            }
            catch (Exception e)
            {
                MessageBox.Show(e.Message, "NotmyFault 卸载", MessageBoxButton.OK, MessageBoxImage.Error);
                return 1;
            }
        }
    }

    public sealed class UninstallerWindow : Window
    {
        private string directory;
        private readonly bool preview;
        private readonly Grid pageHost;
        private readonly Brush ink = SetupWindowLayout.BrushOf("#E5E1EA");
        private readonly Brush muted = SetupWindowLayout.BrushOf("#C8C4CF");
        private readonly FontFamily displayFont;
        private readonly SetupWindowLayout layout;
        private TextBlock progressMessage;
        private TextBlock progressDetail;
        private bool uninstalling;

        public UninstallerWindow(string installationDirectory, bool previewMode = false)
        {
            directory = installationDirectory;
            preview = previewMode;
            Title = preview ? "NotmyFault 卸载界面预览" : "NotmyFault 卸载";
            Width = 920;
            Height = 660;
            MinWidth = 800;
            MinHeight = 640;
            WindowStartupLocation = WindowStartupLocation.CenterScreen;
            Background = SetupWindowLayout.BrushOf("#121318");
            Foreground = ink;
            string fonts = "/" + typeof(UninstallerWindow).Assembly.GetName().Name + ";component/fonts/#";
            FontFamily = new FontFamily(new Uri("pack://application:,,,/"), fonts + "Roboto, Microsoft YaHei UI");
            displayFont = new FontFamily(new Uri("pack://application:,,,/"), fonts + "Google Sans Flex, Microsoft YaHei UI");
            FontSize = 14;
            UseLayoutRounding = true;
            TextOptions.SetTextFormattingMode(this, TextFormattingMode.Display);
            Resources = (ResourceDictionary)XamlReader.Parse(SetupWindowLayout.Styles);
            layout = new SetupWindowLayout(Resources, displayFont, ink, muted);
            pageHost = new Grid { Margin = new Thickness(52, 44, 52, 28) };
            var root = new Grid { Background = Background, ClipToBounds = true };
            root.Children.Add(pageHost);
            Content = root;
            SourceInitialized += delegate
            {
                int enabled = 1;
                DwmSetWindowAttribute(new System.Windows.Interop.WindowInteropHelper(this).Handle, 20, ref enabled, sizeof(int));
            };
            Closing += delegate(object sender, System.ComponentModel.CancelEventArgs e) { if (uninstalling) e.Cancel = true; };
            ShowConfirmation();
        }

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr window, int attribute, ref int value, int size);

        private void ChangePage(Grid page, bool focusPage = false)
        {
            bool animate = pageHost.Children.Count > 0 && SystemParameters.ClientAreaAnimation;
            SetupWindowLayout.ShowPage(pageHost, page, animate, focusPage);
        }

        private void ShowConfirmation()
        {
            bool pending = !preview && InstallMaintenance.HasUninstallPlan(directory);
            var page = layout.Page(pending ? "继续卸载 NotmyFault" : "卸载 NotmyFault", pending ?
                "程序文件可能已部分移除，将继续清理剩余文件和启动入口。" : "将移除 NotmyFault 程序、Python 运行环境、启动入口和签名密钥。");
            var body = new StackPanel { Margin = new Thickness(0, 12, 0, 0) };
            body.Children.Add(SetupWindowLayout.Text("保留您的数据", 21, ink));
            var data = SetupWindowLayout.Text("配置和规则会保留。安装目录中需保留的用户文件将移到同级的独立文件夹。签名密钥将删除，如需继续使用原密钥，请先备份 app\\.private。", 15, muted);
            data.Margin = new Thickness(0, 12, 0, 28);
            body.Children.Add(data);
            body.Children.Add(SetupWindowLayout.Text("安装位置", 13, muted));
            var path = SetupWindowLayout.Text(directory, 15, ink);
            path.Margin = new Thickness(0, 8, 0, 0);
            body.Children.Add(path);
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            layout.Footer(page, layout.Button("取消", Close, false), layout.Button(preview ? "预览卸载" : pending ? "继续卸载" : "卸载", BeginUninstall, true));
            ChangePage(page);
        }

        private async void BeginUninstall()
        {
            if (uninstalling) return;
            uninstalling = true;
            ShowProgress();
            try
            {
                string retained;
                if (preview) retained = directory;
                else retained = await InstallMaintenance.UninstallAsync(directory, new Progress<InstallProgress>(UpdateProgress), CancellationToken.None);
                uninstalling = false;
                if (!preview) ShowComplete(retained);
            }
            catch (Exception e)
            {
                uninstalling = false;
                var failure = e as UninstallFailure;
                if (failure != null) directory = failure.RetryDirectory;
                ShowFailure(e.Message);
            }
        }

        private void ShowProgress()
        {
            var page = layout.Page("正在卸载 NotmyFault", "正在移除程序文件，请稍候。");
            var body = new StackPanel { Margin = new Thickness(0, 14, 0, 0) };
            var row = new StackPanel { Orientation = Orientation.Horizontal };
            var indicator = new StepIndicator { Width = 26, Height = 26, Margin = new Thickness(0, 0, 20, 0), VerticalAlignment = VerticalAlignment.Center };
            indicator.SetState(1);
            AutomationProperties.SetName(indicator, "正在卸载");
            row.Children.Add(indicator);
            progressMessage = SetupWindowLayout.Text("正在准备卸载…", 19, ink);
            row.Children.Add(progressMessage);
            body.Children.Add(row);
            progressDetail = SetupWindowLayout.Text("", 13, muted);
            progressDetail.Margin = new Thickness(46, 14, 0, 0);
            progressDetail.TextTrimming = TextTrimming.CharacterEllipsis;
            progressDetail.TextWrapping = TextWrapping.NoWrap;
            body.Children.Add(progressDetail);
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            if (preview) layout.Footer(page, layout.Button("返回", ShowConfirmation, false), layout.Button("预览完成", delegate { ShowComplete(directory); }, true));
            ChangePage(page, true);
        }

        private void UpdateProgress(InstallProgress progress)
        {
            progressMessage.Text = progress.Message ?? "正在卸载…";
            progressDetail.Text = progress.Detail ?? "";
        }

        private void ShowComplete(string retained)
        {
            var page = layout.Page("卸载已完成", "NotmyFault 的程序文件和启动入口已移除，原安装路径可再次安装。");
            var body = new StackPanel { Margin = new Thickness(0, 12, 0, 0) };
            var check = new StepIndicator { Width = 52, Height = 52, HorizontalAlignment = HorizontalAlignment.Left,
                Margin = new Thickness(0, 0, 0, 24) };
            check.Loaded += delegate { check.SetState(2); };
            body.Children.Add(check);
            body.Children.Add(SetupWindowLayout.Text("签名密钥已删除，配置、规则和用户插件已保留。", 15, muted));
            if (!String.IsNullOrEmpty(retained))
            {
                var path = SetupWindowLayout.Text("原安装目录中的用户文件已移至：\n" + retained, 14, muted);
                path.Margin = new Thickness(0, 18, 0, 0);
                body.Children.Add(path);
            }
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            var finish = layout.Button("完成", Close, true);
            finish.IsDefault = true;
            layout.Footer(page, preview ? layout.Button("返回", ShowConfirmation, false) : null, finish);
            ChangePage(page);
        }

        private void ShowFailure(string message)
        {
            var page = layout.Page("卸载未完成", message);
            layout.Footer(page, layout.Button("退出", Close, false), layout.Button("重试", BeginUninstall, true));
            ChangePage(page);
        }
    }
}
