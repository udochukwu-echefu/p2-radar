// P2 Radar for Windows: a native window around the P2 Radar engine (radar.py),
// the counterpart of mac-app/main.swift.
// The app starts the engine on a free local port, shows its dashboard in
// WebView2, lives in the system tray, watches the user folder so cards update
// live, and stops the engine on quit. `"P2 Radar.exe" --notify` runs the weekly
// check and shows a notification.

using System.Diagnostics;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Radar;

static class Engine
{
    public static readonly string Dir = Path.Combine(AppContext.BaseDirectory, "engine");
    public static readonly string Script = Path.Combine(Dir, "radar.py");
    public static readonly string Secret = Guid.NewGuid().ToString("N");   // lets only this app report file changes
    public static readonly string DataDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Radar");

    /// <summary>python.exe (or the py launcher), skipping the Microsoft Store stub.</summary>
    public static (string exe, string[] pre)? FindPython()
    {
        var env = Environment.GetEnvironmentVariable("RADAR_PYTHON");
        if (!string.IsNullOrEmpty(env) && File.Exists(env)) return (env, Array.Empty<string>());
        var bundled = Path.Combine(AppContext.BaseDirectory, "python", "python.exe");   // the app's own copy
        if (File.Exists(bundled)) return (bundled, Array.Empty<string>());
        var local = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Python");
        if (Directory.Exists(local))
        {
            var best = Directory.GetDirectories(local, "Python3*").OrderByDescending(d => d.Length).ThenByDescending(d => d).Select(d => Path.Combine(d, "python.exe")).FirstOrDefault(File.Exists);
            if (best != null) return (best, Array.Empty<string>());
        }
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            if (string.IsNullOrWhiteSpace(dir) || dir.Contains("WindowsApps", StringComparison.OrdinalIgnoreCase)) continue;
            var py = Path.Combine(dir.Trim(), "py.exe");
            if (File.Exists(py)) return (py, new[] { "-3" });
            var p = Path.Combine(dir.Trim(), "python.exe");
            if (File.Exists(p)) return (p, Array.Empty<string>());
        }
        var winPy = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "py.exe");
        return File.Exists(winPy) ? (winPy, new[] { "-3" }) : null;
    }

    public static ProcessStartInfo Start(params string[] args)
    {
        var (exe, pre) = FindPython() ?? throw new InvalidOperationException("P2 Radar needs Python 3. Install it from python.org, then open P2 Radar again.");
        var psi = new ProcessStartInfo(exe)
        {
            WorkingDirectory = Dir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var a in pre) psi.ArgumentList.Add(a);
        psi.ArgumentList.Add(Script);
        foreach (var a in args) psi.ArgumentList.Add(a);
        psi.Environment["PYTHONUNBUFFERED"] = "1";
        psi.Environment["PYTHONDONTWRITEBYTECODE"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        psi.Environment["RADAR_APP"] = "1";
        psi.Environment["RADAR_APP_SECRET"] = Secret;
        return psi;
    }
}

static class Native
{
    [DllImport("dwmapi.dll")]
    static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

    /// <summary>Dark or light title bar (Windows 10 20H1 and later).</summary>
    public static void TitleBar(IntPtr hwnd, bool dark)
    {
        int v = dark ? 1 : 0;
        if (DwmSetWindowAttribute(hwnd, 20, ref v, sizeof(int)) != 0) DwmSetWindowAttribute(hwnd, 19, ref v, sizeof(int));
    }

    public static void OpenExternal(string url) => Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
}

sealed class MainForm : Form
{
    readonly WebView2 web = new() { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.FromArgb(10, 10, 10) };
    readonly NotifyIcon tray = new();
    readonly HttpClient http = new() { Timeout = TimeSpan.FromSeconds(20) };
    readonly System.Windows.Forms.Timer flushTimer = new() { Interval = 1200 };
    readonly System.Windows.Forms.Timer summaryTimer = new() { Interval = 60_000 };
    readonly HashSet<string> pending = new(StringComparer.OrdinalIgnoreCase);
    readonly object pendingLock = new();
    readonly StringBuilder stderr = new();
    Process? engine;
    FileSystemWatcher? watcher;
    int? port;
    bool quitting, toldAboutTray;
    JsonElement summary;

    public MainForm()
    {
        Text = "P2 Radar";
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        BackColor = Color.FromArgb(10, 10, 10);
        MinimumSize = new Size(720, 540);
        Size = new Size(1320, 880);
        StartPosition = FormStartPosition.CenterScreen;
        Controls.Add(web);

        tray.Icon = Icon;
        tray.Text = "P2 Radar";
        tray.Visible = true;
        tray.ContextMenuStrip = new ContextMenuStrip();
        tray.ContextMenuStrip.Opening += (_, _) => BuildTrayMenu();
        tray.DoubleClick += (_, _) => ShowMain();
        BuildTrayMenu();

        flushTimer.Tick += (_, _) => FlushChanges();
        summaryTimer.Tick += async (_, _) => await RefreshSummary();
        summaryTimer.Start();
    }

    protected override async void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        Native.TitleBar(Handle, true);
        var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(Engine.DataDir, "WebView2"));
        await web.EnsureCoreWebView2Async(env);
        var core = web.CoreWebView2;
        core.Settings.AreBrowserAcceleratorKeysEnabled = false;   // Ctrl+R rescans instead of reloading
        core.Settings.IsStatusBarEnabled = false;
        core.WebMessageReceived += OnMessage;
        core.NavigationStarting += (_, a) =>
        {
            var uri = new Uri(a.Uri);
            bool local = uri.Scheme == "data" || uri.Scheme == "about" || (uri.Host == "127.0.0.1" && uri.Port == port);
            if (local) return;
            a.Cancel = true;
            if (uri.Scheme is "http" or "https") Native.OpenExternal(a.Uri);   // a dropped file is ignored instead
        };
        core.NewWindowRequested += (_, a) => { a.Handled = true; Native.OpenExternal(a.Uri); };
        core.NavigateToString(StatusPage("Starting P2 Radar…", "Scanning your projects and folders.", true));
        StartEngine();
        StartWatching();
    }

    // ------------------------------------------------------------ engine

    void StartEngine()
    {
        try
        {
            engine = new Process { StartInfo = Engine.Start("serve", "--no-open", "--port", "0"), EnableRaisingEvents = true };
            engine.OutputDataReceived += (_, a) =>
            {
                if (a.Data is null || port is not null) return;
                var m = Regex.Match(a.Data, @"RADAR_PORT=(\d+)");
                if (m.Success) BeginInvoke(() => EngineReady(int.Parse(m.Groups[1].Value)));
            };
            engine.ErrorDataReceived += (_, a) => { if (a.Data != null) lock (stderr) { stderr.AppendLine(a.Data); if (stderr.Length > 4000) stderr.Remove(0, stderr.Length - 4000); } };
            engine.Exited += (_, _) => BeginInvoke(() =>
            {
                if (quitting) return;
                string tail; lock (stderr) tail = stderr.ToString();
                web.CoreWebView2?.NavigateToString(StatusPage("P2 Radar stopped unexpectedly",
                    "Use Restart Engine in the tray menu.<pre>" + System.Net.WebUtility.HtmlEncode(tail[Math.Max(0, tail.Length - 1200)..]) + "</pre>", false));
            });
            engine.Start();
            engine.BeginOutputReadLine();
            engine.BeginErrorReadLine();
        }
        catch (Exception ex)
        {
            web.CoreWebView2?.NavigateToString(StatusPage("Couldn't start P2 Radar", System.Net.WebUtility.HtmlEncode(ex.Message), false));
        }
    }

    void EngineReady(int p)
    {
        port = p;
        web.CoreWebView2.Navigate($"http://127.0.0.1:{p}/");
        _ = RefreshSummary();
    }

    void RestartEngine()
    {
        quitting = true;
        try { if (engine is { HasExited: false }) { engine.Kill(true); engine.WaitForExit(5000); } } catch { }
        quitting = false;
        port = null;
        lock (stderr) stderr.Clear();
        web.CoreWebView2?.NavigateToString(StatusPage("Restarting P2 Radar…", "One moment.", true));
        StartEngine();
    }

    // ------------------------------------------------------------ live file watching

    void StartWatching()
    {
        try
        {
            watcher = new FileSystemWatcher(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile))
            {
                IncludeSubdirectories = true,
                InternalBufferSize = 64 * 1024,
                NotifyFilter = NotifyFilters.FileName | NotifyFilters.DirectoryName | NotifyFilters.LastWrite | NotifyFilters.Size,
            };
            FileSystemEventHandler h = (_, a) => Note(a.FullPath);
            watcher.Changed += h; watcher.Created += h; watcher.Deleted += h;
            watcher.Renamed += (_, a) => { Note(a.OldFullPath); Note(a.FullPath); };
            watcher.Error += (_, _) => { };   // buffer overflow: the engine's own git polling still catches commits
            watcher.EnableRaisingEvents = true;
        }
        catch { /* no live updates; Rescan still works */ }
    }

    static readonly string[] Skip = { @"\AppData\", @"\node_modules\", @"\.git\objects\", @"\$Recycle.Bin", @"\NTUSER", @"\.cache\", @"\npm-cache\" };

    void Note(string path)
    {
        if (Skip.Any(s => path.Contains(s, StringComparison.OrdinalIgnoreCase))) return;
        lock (pendingLock) pending.Add(path);
        BeginInvoke(() => { flushTimer.Stop(); flushTimer.Start(); });
    }

    async void FlushChanges()
    {
        flushTimer.Stop();
        if (port is null) return;
        string[] paths;
        lock (pendingLock) { paths = pending.Take(2000).ToArray(); pending.Clear(); }
        if (paths.Length == 0) return;
        var req = new HttpRequestMessage(HttpMethod.Post, $"http://127.0.0.1:{port}/api/changed")
        {
            Content = new StringContent(JsonSerializer.Serialize(new { paths }), Encoding.UTF8, "application/json"),
        };
        req.Headers.Add("X-Radar-App", Engine.Secret);
        try { await http.SendAsync(req); } catch { }
    }

    // ------------------------------------------------------------ tray

    async Task RefreshSummary()
    {
        if (port is null) return;
        try
        {
            var json = await http.GetStringAsync($"http://127.0.0.1:{port}/api/summary");
            summary = JsonDocument.Parse(json).RootElement.Clone();
            int n = summary.GetProperty("atRisk").GetInt32();
            var text = n > 0 ? $"P2 Radar · {n} need attention" : "P2 Radar · everything is backed up";
            tray.Text = text.Length > 63 ? text[..63] : text;
        }
        catch { }
    }

    void BuildTrayMenu()
    {
        var m = tray.ContextMenuStrip!;
        m.Items.Clear();
        int n = 0, local = 0;
        if (summary.ValueKind == JsonValueKind.Object)
        {
            n = summary.GetProperty("atRisk").GetInt32();
            local = summary.GetProperty("localOnly").GetInt32();
        }
        m.Items.Add(new ToolStripMenuItem(n > 0 ? $"{n} projects need attention" + (local > 0 ? $" · {local} only on this PC" : "") : "Everything is backed up") { Enabled = false });
        if (summary.ValueKind == JsonValueKind.Object && summary.GetProperty("items").GetArrayLength() > 0)
        {
            m.Items.Add(new ToolStripSeparator());
            foreach (var it in summary.GetProperty("items").EnumerateArray().Take(8))
            {
                var path = it.GetProperty("path").GetString()!;
                m.Items.Add($"{it.GetProperty("name").GetString()}  —  {it.GetProperty("line").GetString()}", null, (_, _) =>
                {
                    ShowMain();
                    _ = web.CoreWebView2?.ExecuteScriptAsync($"window.radarFocus && radarFocus({JsonSerializer.Serialize(path)})");
                });
            }
        }
        m.Items.Add(new ToolStripSeparator());
        m.Items.Add("Open P2 Radar", null, (_, _) => ShowMain());
        m.Items.Add("Go to Project…", null, (_, _) => { ShowMain(); _ = web.CoreWebView2?.ExecuteScriptAsync("window.openPalette && openPalette()"); });
        m.Items.Add("Rescan", null, (_, _) => _ = web.CoreWebView2?.ExecuteScriptAsync("typeof rescan === 'function' && rescan()"));
        m.Items.Add("Restart Engine", null, (_, _) => RestartEngine());
        m.Items.Add(new ToolStripSeparator());
        m.Items.Add("Quit P2 Radar", null, (_, _) => Quit());
    }

    public void ShowMain()
    {
        Show();
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Activate();
    }

    void Quit()
    {
        quitting = true;
        tray.Visible = false;
        Close();
    }

    // ------------------------------------------------------------ page messages

    void OnMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs a)
    {
        try
        {
            using var doc = JsonDocument.Parse(a.WebMessageAsJson);
            var r = doc.RootElement;
            if (r.TryGetProperty("atRisk", out var n))
            {
                int count = n.GetInt32();
                var text = count > 0 ? $"P2 Radar · {count} need attention" : "P2 Radar · everything is backed up";
                tray.Text = text.Length > 63 ? text[..63] : text;
                _ = RefreshSummary();
            }
            if (r.TryGetProperty("theme", out var t))
            {
                bool dark = t.GetString() != "light";
                Native.TitleBar(Handle, dark);
                BackColor = dark ? Color.FromArgb(10, 10, 10) : Color.FromArgb(246, 246, 248);
            }
        }
        catch { }
    }

    // ------------------------------------------------------------ lifecycle

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (!quitting && e.CloseReason == CloseReason.UserClosing)
        {
            e.Cancel = true;   // keep running in the tray
            Hide();
            if (!toldAboutTray)
            {
                tray.ShowBalloonTip(4000, "P2 Radar is still running", "Find it in the system tray. Right-click it to quit.", ToolTipIcon.Info);
                toldAboutTray = true;
            }
            return;
        }
        quitting = true;
        watcher?.Dispose();
        try { if (engine is { HasExited: false }) engine.Kill(true); } catch { }
        tray.Visible = false;
        base.OnFormClosing(e);
    }

    static string StatusPage(string title, string detail, bool spinner) => $$"""
        <!doctype html><meta charset="utf-8"><style>
        :root { color-scheme: dark; }
        html,body { margin:0; height:100%; background:#0a0a0a; color:#f4f4f4; font:15px/1.5 "Segoe UI", system-ui, sans-serif; }
        body { display:grid; place-items:center; text-align:center; }
        h1 { font-size:17px; font-weight:600; margin:16px 0 4px; }
        p { color:#b8b8b8; margin:0 auto; max-width:46ch; }
        pre { text-align:left; white-space:pre-wrap; font:12px Consolas, monospace; color:#999; }
        .s { width:26px; height:26px; margin:0 auto; border-radius:50%; border:2.5px solid #333; border-top-color:#5b8cff; animation:r .9s linear infinite; }
        @keyframes r { to { transform:rotate(360deg) } }
        </style><body><div>{{(spinner ? "<div class=s></div>" : "")}}<h1>{{title}}</h1><p>{{detail}}</p></div></body>
        """;
}

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        if (args.Contains("--notify")) return Notify();

        // One Radar at a time: a second launch brings the first one forward.
        using var mutex = new Mutex(true, "Radar.SingleInstance", out bool first);
        using var show = new EventWaitHandle(false, EventResetMode.AutoReset, "Radar.Show");
        if (!first) { show.Set(); return 0; }

        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        var form = new MainForm();
        var listener = new Thread(() => { while (show.WaitOne()) form.BeginInvoke(form.ShowMain); }) { IsBackground = true };
        listener.Start();
        Application.Run(form);
        return 0;
    }

    /// <summary>The weekly check: run the engine's notify command and show its message.</summary>
    static int Notify()
    {
        try
        {
            var psi = Engine.Start("notify");
            psi.Environment["RADAR_NOTIFY_JSON"] = "1";
            using var p = Process.Start(psi)!;
            var output = p.StandardOutput.ReadToEnd();
            p.WaitForExit();
            Console.Write(output);
            var line = output.Split('\n').FirstOrDefault(l => l.StartsWith("RADAR_NOTIFY="));
            if (line is null) return p.ExitCode;
            using var doc = JsonDocument.Parse(line["RADAR_NOTIFY=".Length..]);
            var body = doc.RootElement.GetProperty("body").GetString() ?? "";

            Application.EnableVisualStyles();
            using var tray = new NotifyIcon { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath), Text = "P2 Radar", Visible = true };
            tray.BalloonTipClicked += (_, _) => Process.Start(new ProcessStartInfo(Application.ExecutablePath) { UseShellExecute = true });
            tray.ShowBalloonTip(10_000, "P2 Radar", body, ToolTipIcon.Info);
            var until = DateTime.Now.AddSeconds(12);
            while (DateTime.Now < until) { Application.DoEvents(); Thread.Sleep(100); }
            tray.Visible = false;
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.Message);
            return 1;
        }
    }
}
