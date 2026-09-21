using System;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace NotmyFault.Setup
{
    internal sealed class SetupWindowLayout
    {
        private readonly ResourceDictionary resources;
        private readonly FontFamily displayFont;
        private readonly Brush ink;
        private readonly Brush muted;
        private readonly bool defaultPrimaryButton;

        internal SetupWindowLayout(ResourceDictionary resources, FontFamily displayFont, Brush ink, Brush muted, bool defaultPrimaryButton = false)
        {
            this.resources = resources;
            this.displayFont = displayFont;
            this.ink = ink;
            this.muted = muted;
            this.defaultPrimaryButton = defaultPrimaryButton;
        }

        internal static FontFamily Initialize(Window window, Brush ink)
        {
            window.Width = 920;
            window.Height = 660;
            window.MinWidth = 800;
            window.MinHeight = 640;
            window.WindowStartupLocation = WindowStartupLocation.CenterScreen;
            window.Background = BrushOf("#121318");
            window.Foreground = ink;
            string fonts = "/" + window.GetType().Assembly.GetName().Name + ";component/fonts/#";
            var fontBase = new Uri("pack://application:,,,/");
            window.FontFamily = new FontFamily(fontBase, fonts + "Roboto, Microsoft YaHei UI");
            window.FontSize = 14;
            window.UseLayoutRounding = true;
            TextOptions.SetTextFormattingMode(window, TextFormattingMode.Display);
            window.Resources = (ResourceDictionary)XamlReader.Parse(Styles);
            window.SourceInitialized += delegate
            {
                int enabled = 1;
                DwmSetWindowAttribute(new System.Windows.Interop.WindowInteropHelper(window).Handle, 20, ref enabled, sizeof(int));
            };
            return new FontFamily(fontBase, fonts + "Google Sans Flex, Microsoft YaHei UI");
        }

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr window, int attribute, ref int value, int size);

        internal static Brush BrushOf(string value)
        {
            var brush = new SolidColorBrush((Color)ColorConverter.ConvertFromString(value));
            brush.Freeze();
            return brush;
        }

        internal static TextBlock Text(string value, double size, Brush color)
        {
            return new TextBlock { Text = value, FontSize = size, Foreground = color,
                TextWrapping = TextWrapping.Wrap, LineHeight = size * 1.5 };
        }

        internal Button Button(string label, Action action, bool filled)
        {
            var button = new Button { Content = label, Style = (Style)resources[filled ? "PrimaryButton" : "PlainButton"],
                IsDefault = defaultPrimaryButton && filled };
            AddButtonMotion(button);
            button.Click += delegate { action(); };
            return button;
        }

        internal Grid Page(string title, string description)
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
            if (!String.IsNullOrEmpty(description))
            {
                var subtitle = Text(description, 14, muted);
                subtitle.Margin = new Thickness(0, 12, 0, 0);
                heading.Children.Add(subtitle);
            }
            page.Children.Add(heading);
            return page;
        }

        internal void Footer(Grid page, Button left, Button right)
        {
            var footer = new Grid { Margin = new Thickness(0, 20, 0, 0) };
            if (left != null) { left.HorizontalAlignment = HorizontalAlignment.Left; footer.Children.Add(left); }
            if (right != null)
            {
                right.HorizontalAlignment = HorizontalAlignment.Right;
                footer.Children.Add(right);
            }
            Grid.SetRow(footer, 2);
            page.Children.Add(footer);
        }

        internal static void ShowPage(Grid host, Grid page, bool animate, bool focusPage, bool back = false)
        {
            host.Children.Clear();
            host.Children.Add(page);
            host.IsEnabled = true;
            host.BeginAnimation(UIElement.OpacityProperty, null);
            host.Opacity = 1;
            if (animate)
            {
                var transform = new TranslateTransform(back ? -20 : 24, 0);
                page.RenderTransform = transform;
                transform.BeginAnimation(TranslateTransform.XProperty,
                    new DoubleAnimation(0, TimeSpan.FromMilliseconds(260)) {
                        EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut } });
                page.BeginAnimation(UIElement.OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(180)));
            }
            if (focusPage) { page.Focusable = true; page.Focus(); }
            else page.MoveFocus(new TraversalRequest(FocusNavigationDirection.First));
        }

        private static void AddButtonMotion(Button button)
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
}
