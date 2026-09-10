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
        private readonly string directory;
        private readonly bool preview;
        private readonly Grid pageHost;
        private readonly Brush ink = BrushOf("#E5E1EA");
        private readonly Brush muted = BrushOf("#C8C4CF");
        private readonly FontFamily displayFont;
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
            Background = BrushOf("#121318");
            Foreground = ink;
            string fonts = "/" + typeof(UninstallerWindow).Assembly.GetName().Name + ";component/fonts/#";
            FontFamily = new FontFamily(new Uri("pack://application:,,,/"), fonts + "Roboto, Microsoft YaHei UI");
            displayFont = new FontFamily(new Uri("pack://application:,,,/"), fonts + "Google Sans Flex, Microsoft YaHei UI");
            FontSize = 14;
            UseLayoutRounding = true;
            TextOptions.SetTextFormattingMode(this, TextFormattingMode.Display);
            Resources = (ResourceDictionary)XamlReader.Parse(InstallerWindow.Styles);
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

        private static Brush BrushOf(string value)
        {
            var brush = new SolidColorBrush((Color)ColorConverter.ConvertFromString(value));
            brush.Freeze();
            return brush;
        }

        private TextBlock Text(string value, double size, Brush color)
        {
            return new TextBlock { Text = value, FontSize = size, Foreground = color,
                TextWrapping = TextWrapping.Wrap, LineHeight = size * 1.5 };
        }

        private Button Button(string title, Action action, bool filled)
        {
            var button = new Button { Content = title, Style = (Style)Resources[filled ? "PrimaryButton" : "PlainButton"] };
            InstallerWindow.AddButtonMotion(button);
            button.Click += delegate { action(); };
            return button;
        }

        private Grid Page(string title, string description)
        {
            var page = new Grid();
            page.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            page.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            page.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 30) };
            var headline = Text(title, 32, ink);
            headline.FontFamily = displayFont;
            headline.FontWeight = FontWeight.FromOpenTypeWeight(650);
            header.Children.Add(headline);
            var explanation = Text(description, 14, muted);
            explanation.Margin = new Thickness(0, 12, 0, 0);
            header.Children.Add(explanation);
            page.Children.Add(header);
            return page;
        }

        private void Footer(Grid page, Button left, Button right)
        {
            var footer = new Grid { Margin = new Thickness(0, 20, 0, 0) };
            if (left != null) { left.HorizontalAlignment = HorizontalAlignment.Left; footer.Children.Add(left); }
            if (right != null) { right.HorizontalAlignment = HorizontalAlignment.Right; footer.Children.Add(right); }
            Grid.SetRow(footer, 2);
            page.Children.Add(footer);
        }

        private void ChangePage(Grid page, bool focusPage = false)
        {
            bool animate = pageHost.Children.Count > 0 && SystemParameters.ClientAreaAnimation;
            pageHost.Children.Clear();
            pageHost.Children.Add(page);
            if (animate)
            {
                var transform = new TranslateTransform(24, 0);
                page.RenderTransform = transform;
                transform.BeginAnimation(TranslateTransform.XProperty, new DoubleAnimation(0, TimeSpan.FromMilliseconds(260))
                    { EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut } });
                page.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(180)));
            }
            if (focusPage) { page.Focusable = true; page.Focus(); }
            else page.MoveFocus(new TraversalRequest(FocusNavigationDirection.First));
        }

        private void ShowConfirmation()
        {
            var page = Page("卸载 NotmyFault", "将移除 NotmyFault 程序、Python 运行环境和启动入口。");
            var body = new StackPanel { Margin = new Thickness(0, 12, 0, 0) };
            body.Children.Add(Text("保留您的数据", 21, ink));
            var data = Text("配置、规则、用户插件和签名密钥会保留。", 15, muted);
            data.Margin = new Thickness(0, 12, 0, 28);
            body.Children.Add(data);
            body.Children.Add(Text("安装位置", 13, muted));
            var path = Text(directory, 15, ink);
            path.Margin = new Thickness(0, 8, 0, 0);
            body.Children.Add(path);
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            Footer(page, Button("取消", Close, false), Button(preview ? "预览卸载" : "卸载", BeginUninstall, true));
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
                ShowFailure(e.Message);
            }
        }

        private void ShowProgress()
        {
            var page = Page("正在卸载 NotmyFault", "正在移除程序文件，请稍候。");
            var body = new StackPanel { Margin = new Thickness(0, 14, 0, 0) };
            var row = new StackPanel { Orientation = Orientation.Horizontal };
            var indicator = new StepIndicator { Width = 26, Height = 26, Margin = new Thickness(0, 0, 20, 0), VerticalAlignment = VerticalAlignment.Center };
            indicator.SetState(1);
            AutomationProperties.SetName(indicator, "正在卸载");
            row.Children.Add(indicator);
            progressMessage = Text("正在准备卸载…", 19, ink);
            row.Children.Add(progressMessage);
            body.Children.Add(row);
            progressDetail = Text("", 13, muted);
            progressDetail.Margin = new Thickness(46, 14, 0, 0);
            progressDetail.TextTrimming = TextTrimming.CharacterEllipsis;
            progressDetail.TextWrapping = TextWrapping.NoWrap;
            body.Children.Add(progressDetail);
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            if (preview) Footer(page, Button("返回", ShowConfirmation, false), Button("预览完成", delegate { ShowComplete(directory); }, true));
            ChangePage(page, true);
        }

        private void UpdateProgress(InstallProgress progress)
        {
            progressMessage.Text = progress.Message ?? "正在卸载…";
            progressDetail.Text = progress.Detail ?? "";
        }

        private void ShowComplete(string retained)
        {
            var page = Page("卸载已完成", "NotmyFault 的程序文件和启动入口已移除。");
            var body = new StackPanel { Margin = new Thickness(0, 12, 0, 0) };
            var check = new StepIndicator { Width = 52, Height = 52, HorizontalAlignment = HorizontalAlignment.Left,
                Margin = new Thickness(0, 0, 0, 24) };
            check.Loaded += delegate { check.SetState(2); };
            body.Children.Add(check);
            body.Children.Add(Text("配置、规则、用户插件和签名密钥已保留。", 15, muted));
            if (!String.IsNullOrEmpty(retained))
            {
                var path = Text("安装目录中的保留文件：\n" + retained, 14, muted);
                path.Margin = new Thickness(0, 18, 0, 0);
                body.Children.Add(path);
            }
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            var finish = Button("完成", Close, true);
            finish.IsDefault = true;
            Footer(page, preview ? Button("返回", ShowConfirmation, false) : null, finish);
            ChangePage(page);
        }

        private void ShowFailure(string message)
        {
            var page = Page("卸载未完成", message);
            Footer(page, Button("退出", Close, false), Button("重试", BeginUninstall, true));
            ChangePage(page);
        }
    }
}
