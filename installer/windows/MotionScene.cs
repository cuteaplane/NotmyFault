using System;
using System.Diagnostics;
using System.Windows;
using System.Windows.Media;

namespace NotmyFault.Setup
{
    public sealed class MotionScene : FrameworkElement
    {
        private readonly Stopwatch clock = Stopwatch.StartNew();
        private bool subscribed;
        private double intensity = 1;
        private double targetIntensity = 1;
        private double previousTime;
        private bool active = true;
        public bool ReducedMotion { get; set; }

        public MotionScene()
        {
            IsHitTestVisible = false;
            Loaded += delegate { Subscribe(); };
            Unloaded += delegate { Unsubscribe(); };
        }

        public void SetPage(bool welcome)
        {
            if (welcome) clock.Restart();
            targetIntensity = welcome ? 1 : 0;
            active = welcome;
            if (!SystemParameters.ClientAreaAnimation || ReducedMotion)
            {
                intensity = targetIntensity;
                InvalidateVisual();
            }
            else Subscribe();
        }

        private void Subscribe()
        {
            if (subscribed || !IsLoaded || !SystemParameters.ClientAreaAnimation || ReducedMotion) return;
            previousTime = clock.Elapsed.TotalSeconds;
            CompositionTarget.Rendering += OnFrame;
            subscribed = true;
        }

        private void Unsubscribe()
        {
            if (!subscribed) return;
            CompositionTarget.Rendering -= OnFrame;
            subscribed = false;
        }

        private void OnFrame(object sender, EventArgs args)
        {
            double now = clock.Elapsed.TotalSeconds;
            if (ReducedMotion || !SystemParameters.ClientAreaAnimation) { intensity = targetIntensity; Unsubscribe(); InvalidateVisual(); return; }
            double delta = Math.Min(0.1, now - previousTime);
            if (delta < 1.0 / 35) return;
            previousTime = now;
            intensity += (targetIntensity - intensity) * (1 - Math.Exp(-delta * 5));
            InvalidateVisual();
            if ((!active || now >= 2.4) && Math.Abs(intensity - targetIntensity) < 0.001) Unsubscribe();
        }

        protected override void OnRender(DrawingContext dc)
        {
            base.OnRender(dc);
            if (ActualWidth <= 0 || ActualHeight <= 0) return;
            double t = SystemParameters.ClientAreaAnimation && !ReducedMotion ? Math.Min(2.4, clock.Elapsed.TotalSeconds) : 2.4;
            dc.PushClip(new RectangleGeometry(new Rect(0, 0, ActualWidth, ActualHeight)));
            dc.PushTransform(new ScaleTransform(ActualWidth / 920, ActualHeight / 620));
            dc.PushOpacity(intensity);
            double arrival = 1 - Math.Pow(1 - Math.Min(1, t / 1.8), 3);
            DrawShape(dc, new Point(814 + 64 * (1 - arrival), 121 - 30 * (1 - arrival)),
                128, arrival * 0.85, -30 + 18 * arrival, "#1B2536");
            DrawShape(dc, new Point(862 + 30 * (1 - arrival), 246 + 30 * (1 - arrival)),
                62, 0, 0, "#253047");
            dc.Pop();
            dc.Pop();
            dc.Pop();
        }

        private static void DrawShape(DrawingContext dc, Point center, double radius,
            double morph, double rotation, string color)
        {
            var geometry = new StreamGeometry();
            using (var context = geometry.Open())
            {
                for (int i = 0; i < 160; i++)
                {
                    double angle = i * Math.PI * 2 / 160;
                    double exponent = 2 + 3.5 * morph;
                    double r = radius / Math.Pow(
                        Math.Pow(Math.Abs(Math.Cos(angle)), exponent) +
                        Math.Pow(Math.Abs(Math.Sin(angle)), exponent), 1 / exponent);
                    double rotated = angle + rotation * Math.PI / 180;
                    var point = new Point(center.X + Math.Cos(rotated) * r,
                        center.Y + Math.Sin(rotated) * r);
                    if (i == 0) context.BeginFigure(point, true, true);
                    else context.LineTo(point, true, false);
                }
            }
            geometry.Freeze();
            var brush = new SolidColorBrush((Color)ColorConverter.ConvertFromString(color));
            brush.Freeze();
            dc.DrawGeometry(brush, null, geometry);
        }
    }
}
