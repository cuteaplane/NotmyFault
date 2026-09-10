using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;

namespace NotmyFault.Setup
{
    public static class Program
    {
        [STAThread]
        public static int Main(string[] args)
        {
            var app = new Application();
            app.DispatcherUnhandledException += delegate(object sender, DispatcherUnhandledExceptionEventArgs e)
            {
                MessageBox.Show(e.Exception.Message, "NotmyFault 安装", MessageBoxButton.OK, MessageBoxImage.Error);
                e.Handled = true;
            };
            return app.Run(new InstallerWindow(Array.IndexOf(args, "--preview") >= 0));
        }
    }

    public sealed class InstallerWindow : Window
    {
        private readonly bool preview;
        private readonly MotionScene scene;
        private readonly Grid pageHost;
        private readonly Brush ink = BrushOf("#E5E1EA");
        private readonly Brush muted = BrushOf("#C8C4CF");
        private readonly Brush primary = BrushOf("#A7C9FF");
        private readonly FontFamily displayFont;
        private readonly StepIndicator[] indicators = new StepIndicator[4];
        private readonly TextBlock[] stepLabels = new TextBlock[4];
        private readonly string[] steps = { "配置 Python 环境", "释放程序文件", "完成构建", "完成安装" };
        private TextBox directory;
        private PasswordBox password;
        private PasswordBox confirmation;
        private TextBlock validation;
        private TextBlock detail;
        private TextBlock optionsTitle;
        private TextBlock optionsDescription;
        private TextBlock passwordHint;
        private TextBlock pathHint;
        private FrameworkElement confirmField;
        private Button installButton;
        private TextBlock countdownText;
        private Border countdownFill;
        private Grid countdownTrack;
        private DispatcherTimer countdown;
        private CancellationTokenSource cancellation;
        private InstallResult result;
        private bool installing;
        private bool launching;
        private bool closeAfterCancel;
        private int remaining;
        private int transition;
        private int currentStep;
        private string selectedDirectory = InstallEngine.DefaultDirectory;
        private bool upgrade;

        public InstallerWindow(bool previewMode)
        {
            preview = previewMode;
            Title = preview ? "NotmyFault 安装界面预览" : "NotmyFault 安装";
            Width = 920;
            Height = 660;
            MinWidth = 800;
            MinHeight = 640;
            WindowStartupLocation = WindowStartupLocation.CenterScreen;
            Background = BrushOf("#121318");
            string fontResource = "/" + typeof(InstallerWindow).Assembly.GetName().Name + ";component/fonts/#";
            FontFamily = new FontFamily(new Uri("pack://application:,,,/"), fontResource + "Roboto, Microsoft YaHei UI");
            displayFont = new FontFamily(new Uri("pack://application:,,,/"), fontResource + "Google Sans Flex, Microsoft YaHei UI");
            Foreground = ink;
            FontSize = 14;
            UseLayoutRounding = true;
            TextOptions.SetTextFormattingMode(this, TextFormattingMode.Display);
            Resources = (ResourceDictionary)XamlReader.Parse(Styles);
            var root = new Grid { ClipToBounds = true, Background = Background };
            scene = new MotionScene();
            root.Children.Add(scene);
            pageHost = new Grid { Margin = new Thickness(52, 44, 52, 28) };
            root.Children.Add(pageHost);
            Content = root;
            string installed = preview ? null : InstallEngine.FindInstalledDirectory();
            if (!String.IsNullOrEmpty(installed)) selectedDirectory = installed;
            ShowWelcome();
            SourceInitialized += delegate
            {
                int enabled = 1;
                DwmSetWindowAttribute(new System.Windows.Interop.WindowInteropHelper(this).Handle, 20, ref enabled, sizeof(int));
            };
            Closing += OnClosing;
            Closed += delegate { StopCountdown(); };
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

        private Button Button(string label, Action action, bool filled)
        {
            var button = new Button { Content = label, Style = (Style)Resources[filled ? "PrimaryButton" : "PlainButton"] };
            AddButtonMotion(button);
            button.Click += delegate { action(); };
            return button;
        }

        internal static void AddButtonMotion(Button button)
        {
            var scale = new ScaleTransform(1, 1);
            button.RenderTransform = scale;
            button.RenderTransformOrigin = new Point(0.5, 0.5);
            Action<bool> press = delegate(bool down)
            {
                if (!SystemParameters.ClientAreaAnimation) return;
                var animation = new DoubleAnimation(down ? 0.96 : 1, TimeSpan.FromMilliseconds(down ? 95 : 180))
                    { EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut } };
                scale.BeginAnimation(ScaleTransform.ScaleXProperty, animation);
                scale.BeginAnimation(ScaleTransform.ScaleYProperty, animation);
            };
            button.PreviewMouseLeftButtonDown += delegate { press(true); };
            button.PreviewMouseLeftButtonUp += delegate { press(false); };
            button.LostMouseCapture += delegate { press(false); };
            button.PreviewKeyDown += delegate(object sender, KeyEventArgs e) { if (e.Key == Key.Space || e.Key == Key.Enter) press(true); };
            button.PreviewKeyUp += delegate(object sender, KeyEventArgs e) { if (e.Key == Key.Space || e.Key == Key.Enter) press(false); };
        }

        private Grid Page(string title, string description)
        {
            var page = new Grid();
            page.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            page.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            page.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var heading = new StackPanel { Margin = new Thickness(0, 0, 0, 30) };
            var headline = Text(title, 32, ink);
            headline.FontFamily = displayFont;
            headline.FontWeight = FontWeight.FromOpenTypeWeight(650);
            heading.Children.Add(headline);
            if (!string.IsNullOrEmpty(description))
            {
                var subtitle = Text(description, 14, muted);
                subtitle.Margin = new Thickness(0, 12, 0, 0);
                heading.Children.Add(subtitle);
            }
            page.Children.Add(heading);
            return page;
        }

        private void Footer(Grid page, Button left, Button right)
        {
            var footer = new Grid { Margin = new Thickness(0, 20, 0, 0) };
            if (left != null) { left.HorizontalAlignment = HorizontalAlignment.Left; footer.Children.Add(left); }
            if (right != null)
            {
                right.HorizontalAlignment = HorizontalAlignment.Right;
                right.IsDefault = right.Style == Resources["PrimaryButton"];
                footer.Children.Add(right);
            }
            Grid.SetRow(footer, 2);
            page.Children.Add(footer);
        }

        private async void ChangePage(Grid page, bool first, bool focusPage = false, bool back = false)
        {
            int token = ++transition;
            StopCountdown();
            scene.SetPage(first);
            bool animate = SystemParameters.ClientAreaAnimation && pageHost.Children.Count != 0;
            if (animate)
            {
                pageHost.IsEnabled = false;
                pageHost.BeginAnimation(OpacityProperty, new DoubleAnimation(0, TimeSpan.FromMilliseconds(100)));
                await Task.Delay(100);
                if (token != transition) return;
            }
            pageHost.Children.Clear();
            pageHost.Children.Add(page);
            pageHost.IsEnabled = true;
            pageHost.BeginAnimation(OpacityProperty, null);
            pageHost.Opacity = 1;
            if (animate)
            {
                var transform = new TranslateTransform(back ? -20 : 24, 0);
                page.RenderTransform = transform;
                transform.BeginAnimation(TranslateTransform.XProperty,
                    new DoubleAnimation(0, TimeSpan.FromMilliseconds(260)) {
                        EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut } });
                page.BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(180)));
            }
            if (focusPage) { page.Focusable = true; page.Focus(); }
            else page.MoveFocus(new TraversalRequest(FocusNavigationDirection.First));
        }

        private void ShowWelcome()
        {
            var page = new Grid();
            page.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            page.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            page.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var body = new StackPanel { VerticalAlignment = VerticalAlignment.Center,
                HorizontalAlignment = HorizontalAlignment.Left, MaxWidth = 540, Margin = new Thickness(0, 0, 0, 34) };
            var title = Text("欢迎使用\nNotmyFault", 44, ink);
            title.LineHeight = 56;
            title.FontFamily = displayFont;
            title.FontWeight = FontWeight.FromOpenTypeWeight(650);
            body.Children.Add(title);
            var introduction = Text("本程序将协助您安装 NotmyFault、配置运行环境，并在安装完成后引导您进行初次设置。", 16, muted);
            introduction.MaxWidth = 430;
            introduction.HorizontalAlignment = HorizontalAlignment.Left;
            introduction.Margin = new Thickness(0, 24, 0, 0);
            body.Children.Add(introduction);
            upgrade = !preview && InstallEngine.IsInstalledDirectory(selectedDirectory);
            if (upgrade)
            {
                var installed = Text("已安装 " + InstallEngine.GetInstalledVersion(selectedDirectory) + " → " + InstallEngine.PackageVersion + "\n继续以升级现有安装，您的配置和签名密钥会保留。", 14, primary);
                installed.Margin = new Thickness(0, 24, 0, 0);
                body.Children.Add(installed);
            }
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            Footer(page, Button("开源许可证", ShowLicense, false), Button(upgrade ? "继续升级" : "开始安装", ShowOptions, true));
            ChangePage(page, true, false, true);
        }

        private StackPanel Field(string label, Control control)
        {
            var field = new StackPanel();
            var caption = Text(label, 13, muted);
            caption.Margin = new Thickness(2, 0, 0, 8);
            field.Children.Add(caption);
            AutomationProperties.SetName(control, label);
            field.Children.Add(control);
            return field;
        }

        private void ShowOptions()
        {
            var page = Page("安装 NotmyFault", "创建签名密码，并选择安装位置。");
            var heading = (StackPanel)page.Children[0];
            optionsTitle = (TextBlock)heading.Children[0];
            optionsDescription = (TextBlock)heading.Children[1];
            var body = new StackPanel();
            var passwords = new Grid();
            passwords.ColumnDefinitions.Add(new ColumnDefinition());
            passwords.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(20) });
            passwords.ColumnDefinitions.Add(new ColumnDefinition());
            password = new PasswordBox { Style = (Style)Resources["PasswordField"] };
            confirmation = new PasswordBox { Style = (Style)Resources["PasswordField"] };
            passwords.Children.Add(Field("签名密码", password));
            confirmField = Field("确认密码", confirmation);
            Grid.SetColumn(confirmField, 2);
            passwords.Children.Add(confirmField);
            body.Children.Add(passwords);
            passwordHint = Text("此密码用于保护签名私钥，修改插件或重新构建时需要使用。", 13, muted);
            passwordHint.Margin = new Thickness(2, 10, 0, 28);
            body.Children.Add(passwordHint);
            var pathRow = new Grid();
            pathRow.ColumnDefinitions.Add(new ColumnDefinition());
            pathRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            directory = new TextBox { Text = selectedDirectory, Style = (Style)Resources["TextField"] };
            AutomationProperties.SetName(directory, "安装路径");
            pathRow.Children.Add(directory);
            var browse = Button("更改", Browse, false);
            browse.Margin = new Thickness(12, 0, 0, 0);
            Grid.SetColumn(browse, 1);
            pathRow.Children.Add(browse);
            var pathLabel = Text("安装路径", 13, muted);
            pathLabel.Margin = new Thickness(2, 0, 0, 8);
            body.Children.Add(pathLabel);
            body.Children.Add(pathRow);
            pathHint = Text("默认安装到当前用户的 AppData 文件夹。请选择空文件夹。", 13, muted);
            pathHint.Margin = new Thickness(2, 10, 0, 0);
            body.Children.Add(pathHint);
            validation = Text("", 13, BrushOf("#FFB4AB"));
            validation.Margin = new Thickness(2, 16, 0, 0);
            body.Children.Add(validation);
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            installButton = Button(preview ? "预览安装" : "安装", BeginInstall, true);
            Footer(page, Button("返回", delegate { selectedDirectory = directory.Text.Trim(); password.Clear(); confirmation.Clear(); ShowWelcome(); }, false), installButton);
            directory.TextChanged += delegate { RefreshInstallMode(); };
            RefreshInstallMode();
            ChangePage(page, false);
        }

        private void RefreshInstallMode()
        {
            bool existing = false;
            try { existing = !preview && InstallEngine.IsInstalledDirectory(directory.Text.Trim()); }
            catch (ArgumentException) { }
            catch (NotSupportedException) { }
            catch (IOException) { }
            upgrade = existing;
            optionsTitle.Text = upgrade ? "升级 NotmyFault" : "安装 NotmyFault";
            optionsDescription.Text = upgrade ? InstallEngine.GetInstalledVersion(directory.Text.Trim()) + " → " + InstallEngine.PackageVersion + "，保留现有配置和签名密钥。" : "创建签名密码，并选择安装位置。";
            passwordHint.Text = upgrade ? "请输入现有安装的签名密码，用于解锁原签名私钥。" : "此密码用于保护签名私钥，修改插件或重新构建时需要使用。";
            pathHint.Text = upgrade ? "检测到现有安装，将在此位置更新 NotmyFault。" : "默认安装到当前用户的 AppData 文件夹。请选择空文件夹。";
            confirmField.Visibility = upgrade ? Visibility.Collapsed : Visibility.Visible;
            installButton.Content = preview ? "预览安装" : upgrade ? "升级" : "安装";
        }

        private void Browse()
        {
            using (var dialog = new System.Windows.Forms.FolderBrowserDialog())
            {
                dialog.Description = "选择父文件夹，NotmyFault 将安装到其中的 NotmyFault 文件夹。";
                dialog.ShowNewFolderButton = true;
                if (dialog.ShowDialog() == System.Windows.Forms.DialogResult.OK)
                    directory.Text = Path.Combine(dialog.SelectedPath, "NotmyFault");
            }
        }

        private static bool SamePassword(SecureString first, SecureString second)
        {
            if (first.Length != second.Length) return false;
            IntPtr a = IntPtr.Zero, b = IntPtr.Zero;
            try
            {
                a = Marshal.SecureStringToGlobalAllocUnicode(first);
                b = Marshal.SecureStringToGlobalAllocUnicode(second);
                int difference = 0;
                for (int i = 0; i < first.Length; i++) difference |= Marshal.ReadInt16(a, i * 2) ^ Marshal.ReadInt16(b, i * 2);
                return difference == 0;
            }
            finally
            {
                if (a != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(a);
                if (b != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(b);
            }
        }

        private async void BeginInstall()
        {
            if (installing) return;
            selectedDirectory = directory.Text.Trim();
            if (preview) { ShowProgress(); return; }
            using (SecureString signingPassword = password.SecurePassword)
            using (SecureString repeated = confirmation.SecurePassword)
            {
                if (signingPassword.Length == 0)
                {
                    validation.Text = upgrade ? "请输入现有安装的签名密码。" : "请创建签名密码。";
                    password.Focus();
                    return;
                }
                if (!upgrade && !SamePassword(signingPassword, repeated))
                {
                    validation.Text = "两次输入的密码不一致。";
                    confirmation.Focus();
                    return;
                }
                try { selectedDirectory = upgrade ? Path.GetFullPath(selectedDirectory) : InstallEngine.ValidateDirectory(selectedDirectory); }
                catch (Exception e) { validation.Text = e.Message; directory.Focus(); return; }
                installing = true;
                cancellation = new CancellationTokenSource();
                password.Clear();
                confirmation.Clear();
                ShowProgress();
                try
                {
                    var options = new InstallOptions { Directory = selectedDirectory, SigningPassword = signingPassword, Upgrade = upgrade };
                    result = await new InstallEngine().InstallAsync(options, new Progress<InstallProgress>(UpdateProgress), cancellation.Token);
                    installing = false;
                    if (!closeAfterCancel) ShowComplete();
                }
                catch (OperationCanceledException)
                {
                    installing = false;
                    if (!closeAfterCancel) ShowFailure(upgrade ? "升级已取消" : "安装已取消", upgrade ? "已清理本次升级的临时文件，原安装已保留。" : "本次安装产生的文件已清理。");
                }
                catch (Exception e)
                {
                    installing = false;
                    if (!closeAfterCancel) ShowFailure(upgrade ? "升级未完成" : "安装未完成", e.Message);
                }
                finally
                {
                    cancellation.Dispose();
                    cancellation = null;
                    if (closeAfterCancel) Close();
                }
            }
        }

        private void ShowProgress()
        {
            var page = Page(upgrade ? "正在升级 NotmyFault" : "正在安装 NotmyFault", "当前安装器仍在开发，如果出现问题，请按照项目说明手动安装。" +
                (preview ? "\n界面预览，不会执行安装。" : ""));
            var body = new StackPanel { Margin = new Thickness(0, 12, 0, 0) };
            for (int i = 0; i < steps.Length; i++)
            {
                var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 22) };
                indicators[i] = new StepIndicator { Width = 26, Height = 26, Margin = new Thickness(0, 0, 20, 0), VerticalAlignment = VerticalAlignment.Center };
                stepLabels[i] = Text(steps[i], 19, muted);
                row.Children.Add(indicators[i]);
                row.Children.Add(stepLabels[i]);
                body.Children.Add(row);
            }
            detail = Text("", 13, muted);
            detail.Margin = new Thickness(46, 2, 0, 0);
            detail.TextTrimming = TextTrimming.CharacterEllipsis;
            detail.TextWrapping = TextWrapping.NoWrap;
            body.Children.Add(detail);
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            currentStep = preview ? 3 : 0;
            PaintSteps(currentStep, false);
            Footer(page, preview ? Button("返回", ShowOptions, false) : null,
                preview ? Button("预览完成", ShowComplete, true) : Button("取消", Cancel, false));
            ChangePage(page, false, true);
        }

        private void PaintSteps(int step, bool finished)
        {
            for (int i = 0; i < steps.Length; i++)
            {
                bool done = i < step || (i == step && finished);
                bool active = i == step && !finished;
                indicators[i].SetState(done ? 2 : active ? 1 : 0);
                stepLabels[i].Text = steps[i] + (active ? "…" : "");
                stepLabels[i].Foreground = done || active ? ink : BrushOf("#91909B");
                stepLabels[i].FontWeight = active ? FontWeights.SemiBold : FontWeights.Normal;
                AutomationProperties.SetName(indicators[i], done ? "已完成" : active ? "正在进行" : "等待中");
            }
        }

        private void UpdateProgress(InstallProgress update)
        {
            if (!installing || cancellation == null) return;
            currentStep = Math.Max(currentStep, Math.Min(3, update.Step));
            PaintSteps(currentStep, update.Step == currentStep && update.Fraction >= 1);
            detail.Text = update.Message ?? "";
        }

        private void ShowComplete()
        {
            bool upgraded = result != null && result.Upgraded;
            var page = Page(upgraded ? "升级已完成" : "安装已完成", "");
            var body = new StackPanel { Margin = new Thickness(0, 12, 0, 0) };
            var success = new StepIndicator { Width = 52, Height = 52, HorizontalAlignment = HorizontalAlignment.Left,
                Margin = new Thickness(0, 0, 0, 24) };
            AutomationProperties.SetName(success, upgraded ? "升级成功" : "安装成功");
            success.Loaded += delegate { success.SetState(2); };
            body.Children.Add(success);
            countdownText = Text("NotmyFault 将在 10 秒后启动初次配置流程……", 17, muted);
            body.Children.Add(countdownText);
            countdownTrack = new Grid { Height = 6, Margin = new Thickness(0, 32, 0, 0), ClipToBounds = true };
            countdownTrack.Children.Add(new Border { Background = BrushOf("#34353B"), CornerRadius = new CornerRadius(3) });
            countdownFill = new Border { Background = primary, CornerRadius = new CornerRadius(3), Width = 0, HorizontalAlignment = HorizontalAlignment.Left };
            countdownTrack.Children.Add(countdownFill);
            AutomationProperties.SetName(countdownTrack, "启动倒计时");
            countdownTrack.SizeChanged += delegate { UpdateCountdown(); };
            body.Children.Add(countdownTrack);
            if (result != null && !String.IsNullOrEmpty(result.Warning))
            {
                var warning = Text(result.Warning, 13, muted);
                warning.Margin = new Thickness(0, 18, 0, 0);
                body.Children.Add(warning);
            }
            if (preview)
            {
                var note = Text("界面预览，倒计时结束后不会启动程序。", 13, muted);
                note.Margin = new Thickness(0, 18, 0, 0);
                body.Children.Add(note);
            }
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            Footer(page, preview ? Button("返回", ShowWelcome, false) : null, Button(upgraded ? "立即启动" : "立即重启", Launch, true));
            remaining = 10;
            page.Loaded += delegate
            {
                var elapsed = Stopwatch.StartNew();
                countdown = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(100) };
                countdown.Tick += delegate
                {
                    int next = Math.Max(0, 10 - (int)elapsed.Elapsed.TotalSeconds);
                    if (next == remaining) return;
                    remaining = next;
                    UpdateCountdown();
                    if (remaining <= 0) { StopCountdown(); if (!preview) Launch(); }
                };
                UpdateCountdown();
                countdown.Start();
            };
            ChangePage(page, false);
        }

        private void UpdateCountdown()
        {
            if (countdownTrack == null) return;
            double width = countdownTrack.ActualWidth * (10 - remaining) / 10.0;
            if (SystemParameters.ClientAreaAnimation)
                countdownFill.BeginAnimation(WidthProperty, new DoubleAnimation(width, TimeSpan.FromMilliseconds(180))
                    { EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut } });
            else { countdownFill.BeginAnimation(WidthProperty, null); countdownFill.Width = width; }
            bool upgraded = result != null && result.Upgraded;
            countdownText.Text = remaining > 0 ? "NotmyFault 将在 " + remaining + " 秒后" + (upgraded ? "启动……" : "启动初次配置流程……")
                : preview ? "倒计时已结束。" : upgraded ? "正在启动 NotmyFault……" : "正在启动 NotmyFault 初次配置流程……";
        }

        private void StopCountdown()
        {
            if (countdown != null) { countdown.Stop(); countdown = null; }
        }

        private void Launch()
        {
            if (launching) return;
            StopCountdown();
            if (preview) { ShowWelcome(); return; }
            launching = true;
            try { InstallEngine.Launch(result); Close(); }
            catch (Exception e)
            {
                launching = false;
                countdownText.Text = "无法启动 NotmyFault：" + e.Message + "\n请点击“" + (result.Upgraded ? "立即启动" : "立即重启") + "”重试。";
            }
        }

        private void ShowFailure(string title, string message)
        {
            var page = Page(title, message);
            var body = new StackPanel();
            string log = Path.Combine(selectedDirectory, "install.log");
            if (File.Exists(log))
            {
                body.Children.Add(Text("安装日志", 15, ink));
                body.Children.Add(new TextBox { Text = log, IsReadOnly = true, BorderThickness = new Thickness(0),
                    Background = Brushes.Transparent, Foreground = muted, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 10, 0, 20) });
                body.Children.Add(Button("打开日志", delegate { Process.Start("notepad.exe", "\"" + log + "\""); }, false));
            }
            Grid.SetRow(body, 1);
            page.Children.Add(body);
            Footer(page, Button("退出", Close, false), Button("返回安装设置", ShowOptions, true));
            ChangePage(page, false);
        }

        private void Cancel()
        {
            if (cancellation == null || cancellation.IsCancellationRequested) return;
            cancellation.Cancel();
            detail.Text = "正在取消并清理本次安装文件，请稍候。";
        }

        private void OnClosing(object sender, System.ComponentModel.CancelEventArgs e)
        {
            if (!installing) return;
            e.Cancel = true;
            closeAfterCancel = true;
            Cancel();
        }

        private void ShowLicense()
        {
            string text;
            using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("NotmyFault.License"))
            using (var reader = new StreamReader(stream)) text = reader.ReadToEnd();
            new Window { Title = "NotmyFault 开源许可证", Owner = this, Width = 720, Height = 530,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Content = new TextBox { Text = text, IsReadOnly = true, TextWrapping = TextWrapping.Wrap, Background = Background,
                    Foreground = ink, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(24) } }.ShowDialog();
        }

        internal const string Styles = @"<ResourceDictionary
            xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'
            xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml'>
          <Style x:Key='PlainButton' TargetType='Button'>
            <Setter Property='Foreground' Value='#A7C9FF'/><Setter Property='Background' Value='Transparent'/>
            <Setter Property='Padding' Value='24,12'/><Setter Property='MinHeight' Value='46'/>
            <Setter Property='FontSize' Value='14'/><Setter Property='FontWeight' Value='SemiBold'/>
            <Setter Property='BorderThickness' Value='0'/><Setter Property='Cursor' Value='Hand'/>
            <Setter Property='Template'><Setter.Value><ControlTemplate TargetType='Button'>
              <Grid><Border x:Name='Shape' Background='{TemplateBinding Background}' CornerRadius='24'
                Padding='{TemplateBinding Padding}'><ContentPresenter HorizontalAlignment='Center' VerticalAlignment='Center'/></Border>
                <Border x:Name='Focus' CornerRadius='26' Margin='-4' BorderBrush='#A7C9FF' BorderThickness='0'/></Grid>
              <ControlTemplate.Triggers>
                <Trigger Property='IsMouseOver' Value='True'><Setter TargetName='Shape' Property='Opacity' Value='0.8'/></Trigger>
                <Trigger Property='IsPressed' Value='True'><Setter TargetName='Shape' Property='CornerRadius' Value='14'/></Trigger>
                <Trigger Property='IsKeyboardFocused' Value='True'><Setter TargetName='Focus' Property='BorderThickness' Value='2'/></Trigger>
                <Trigger Property='IsEnabled' Value='False'><Setter TargetName='Shape' Property='Opacity' Value='0.45'/></Trigger>
              </ControlTemplate.Triggers>
            </ControlTemplate></Setter.Value></Setter>
          </Style>
          <Style x:Key='PrimaryButton' TargetType='Button' BasedOn='{StaticResource PlainButton}'>
            <Setter Property='Background' Value='#A7C9FF'/><Setter Property='Foreground' Value='#00315F'/>
            <Setter Property='MinWidth' Value='120'/>
          </Style>
          <Style x:Key='Input' TargetType='Control'>
            <Setter Property='Height' Value='48'/><Setter Property='Padding' Value='15,0'/>
            <Setter Property='VerticalContentAlignment' Value='Center'/>
            <Setter Property='Foreground' Value='#E5E1EA'/><Setter Property='Background' Value='#0D0E13'/>
            <Setter Property='BorderBrush' Value='#47464F'/><Setter Property='BorderThickness' Value='1'/>
            <Setter Property='Template'><Setter.Value><ControlTemplate TargetType='Control'>
              <Border x:Name='Field' CornerRadius='14' Background='{TemplateBinding Background}'
                BorderBrush='{TemplateBinding BorderBrush}' BorderThickness='{TemplateBinding BorderThickness}'>
                <ScrollViewer x:Name='PART_ContentHost' Margin='{TemplateBinding Padding}'
                    VerticalAlignment='{TemplateBinding VerticalContentAlignment}'/>
              </Border>
              <ControlTemplate.Triggers>
                <Trigger Property='IsKeyboardFocusWithin' Value='True'>
                  <Setter TargetName='Field' Property='BorderBrush' Value='#A7C9FF'/>
                  <Setter TargetName='Field' Property='BorderThickness' Value='2'/>
                </Trigger>
              </ControlTemplate.Triggers>
            </ControlTemplate></Setter.Value></Setter>
          </Style>
          <Style x:Key='PasswordField' TargetType='PasswordBox' BasedOn='{StaticResource Input}'>
            <Setter Property='CaretBrush' Value='#A7C9FF'/>
          </Style>
          <Style x:Key='TextField' TargetType='TextBox' BasedOn='{StaticResource Input}'>
            <Setter Property='CaretBrush' Value='#A7C9FF'/>
          </Style>
        </ResourceDictionary>";
    }

    public sealed class StepIndicator : FrameworkElement
    {
        private int state;
        private readonly RotateTransform rotation = new RotateTransform(0, 13, 13);
        private static readonly DependencyProperty CompletionProperty = DependencyProperty.Register(
            "Completion", typeof(double), typeof(StepIndicator), new FrameworkPropertyMetadata(1.0, FrameworkPropertyMetadataOptions.AffectsRender));
        public StepIndicator()
        {
            Loaded += delegate { SystemParameters.StaticPropertyChanged += OnSystemParametersChanged; AnimateState(); };
            Unloaded += delegate { SystemParameters.StaticPropertyChanged -= OnSystemParametersChanged; rotation.BeginAnimation(RotateTransform.AngleProperty, null); };
        }
        private void OnSystemParametersChanged(object sender, System.ComponentModel.PropertyChangedEventArgs e)
        {
            if (e.PropertyName == "ClientAreaAnimation") AnimateState();
        }
        public void SetState(int value)
        {
            if (state == value) return;
            state = value;
            AnimateState();
            InvalidateVisual();
        }
        private void AnimateState()
        {
            rotation.BeginAnimation(RotateTransform.AngleProperty, null);
            BeginAnimation(CompletionProperty, null);
            if (state == 1 && SystemParameters.ClientAreaAnimation)
                rotation.BeginAnimation(RotateTransform.AngleProperty,
                    new DoubleAnimation(0, 360, TimeSpan.FromMilliseconds(1150)) { RepeatBehavior = RepeatBehavior.Forever });
            if (state == 2 && SystemParameters.ClientAreaAnimation)
                BeginAnimation(CompletionProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(300))
                    { EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut } });
        }
        protected override void OnRender(DrawingContext dc)
        {
            dc.PushTransform(new ScaleTransform(ActualWidth / 26, ActualHeight / 26));
            var color = new SolidColorBrush((Color)ColorConverter.ConvertFromString(state == 0 ? "#47464F" : "#A7C9FF"));
            var pen = new Pen(color, state == 0 ? 2 : 2.6) { StartLineCap = PenLineCap.Round, EndLineCap = PenLineCap.Round };
            if (state == 0) dc.DrawEllipse(null, pen, new Point(13, 13), 9, 9);
            else if (state == 2)
            {
                var check = new StreamGeometry();
                using (var c = check.Open())
                {
                    double amount = (double)GetValue(CompletionProperty);
                    c.BeginFigure(new Point(5, 13), false, false);
                    double first = Math.Min(1, amount / 0.34);
                    c.LineTo(new Point(5 + 5.5 * first, 13 + 5.5 * first), true, false);
                    if (amount > 0.34)
                    {
                        double second = (amount - 0.34) / 0.66;
                        c.LineTo(new Point(10.5 + 10.5 * second, 18.5 - 11 * second), true, false);
                    }
                }
                dc.DrawGeometry(null, pen, check);
            }
            else
            {
                var arc = new StreamGeometry();
                using (var c = arc.Open())
                {
                    c.BeginFigure(new Point(13, 3), false, false);
                    c.ArcTo(new Point(3, 13), new Size(10, 10), 0, true, SweepDirection.Clockwise, true, false);
                }
                dc.PushTransform(rotation);
                dc.DrawGeometry(null, pen, arc);
                dc.Pop();
            }
            dc.Pop();
        }
    }
}
