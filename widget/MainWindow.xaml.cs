using Microsoft.Web.WebView2.Core;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Threading;

namespace StackChanWidget;

public partial class MainWindow : Window
{
    private const double WidgetWidth = 976;
    private const double WidgetHeight = 676;
    private readonly string _root;
    private readonly string _stateDirectory;
    private readonly string _positionPath;
    private readonly DispatcherTimer _positionSaveTimer;
    private readonly DispatcherTimer _dashboardRefreshTimer;
    private FileSystemWatcher? _statusWatcher;
    private PointSnapshot? _pendingPosition;

    public MainWindow()
    {
        InitializeComponent();
        _root = FindGatewayRoot();
        _stateDirectory = Path.Combine(_root, "state");
        _positionPath = Path.Combine(_stateDirectory, "desktop_widget_position.json");
        _positionSaveTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(350) };
        _positionSaveTimer.Tick += (_, _) => SavePosition();
        _dashboardRefreshTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(180) };
        _dashboardRefreshTimer.Tick += async (_, _) => await RefreshDashboardAsync();
        RestorePosition();
        LocationChanged += (_, _) => SchedulePositionSave();
        Closed += (_, _) => { SavePosition(); _statusWatcher?.Dispose(); _dashboardRefreshTimer.Stop(); };
        Loaded += async (_, _) => await InitializeBrowserAsync();
    }

    private async Task InitializeBrowserAsync()
    {
        Directory.CreateDirectory(_stateDirectory);
        await Browser.EnsureCoreWebView2Async();
        Browser.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
        Browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
        Browser.CoreWebView2.SetVirtualHostNameToFolderMapping("stackchan.local", _root, CoreWebView2HostResourceAccessKind.Allow);
        Browser.CoreWebView2.AddHostObjectToScript("stackchan", new WidgetBridge(this, _root));
        WriteWidgetHtml();
        Browser.CoreWebView2.Navigate("https://stackchan.local/state/desktop_widget.webview.html");
        WatchTrayStatus();
    }

    private static string FindGatewayRoot()
    {
        for (var current = new DirectoryInfo(AppContext.BaseDirectory); current is not null; current = current.Parent)
        {
            var gateway = Path.Combine(current.FullName, "gateway");
            if (File.Exists(Path.Combine(gateway, "M5 StackChan Desktop Widget.hta"))) return gateway;
        }
        throw new DirectoryNotFoundException("未找到 StackChan gateway 目录。");
    }

    private void WriteWidgetHtml()
    {
        var sourcePath = Path.Combine(_root, "M5 StackChan Desktop Widget.hta");
        var html = File.ReadAllText(sourcePath, new UTF8Encoding(false));
        html = System.Text.RegularExpressions.Regex.Replace(html, @"(?im)^<hta:application\b.*?/>\s*", string.Empty);
        html = html.Replace("<head>", "<head><base href=\"https://stackchan.local/\">");
        var jsRoot = _root.Replace("\\", "\\\\");
        html = System.Text.RegularExpressions.Regex.Replace(
            html,
            @"var widgetPath=.*?var statusPath=root\+'\\\\state\\\\tray_status\.json';",
            $"var root='{jsRoot}';var statusPath=root+'\\\\state\\\\tray_status.json';");
        html = html.Replace("hostScale=1.25", "hostScale=1");
        const string bridgeScript = """
<style>html,body{-webkit-user-select:none;user-select:none}</style>
<script>
var root='__STACKCHAN_ROOT__';var statusPath=root+'\\state\\tray_status.json';
function bridge(){return window.chrome.webview.hostObjects.sync.stackchan;}
function readText(path){try{return bridge().ReadText(path)||'';}catch(e){return '';}}
function runPs(file,args,visible){return bridge().RunPowerShell(file,args||'')>=0;}
function runPwsh(file,args){return bridge().RunPwsh(file,args||'')>=0;}
function runPy(file,args){return bridge().RunPython(file,args||'')>=0;}
function openSettings(){bridge().OpenSettings();}
function setPin(v){var code=bridge().SetPin(!!v);if(code<0){alert('置顶操作未完成，请重试。');return;}pinned=!!v;var b=document.getElementById('pin');b.className=pinned?'toggle':'toggle off';b.title=pinned?'窗口始终置顶：开':'窗口始终置顶：关';}
function fit(){document.getElementById('ui').style.transform='scale(0.63)';}
function lockSize(){fit();}
document.onselectstart=function(){return false;};
</script>
""";
        html = html.Replace("</head>", bridgeScript.Replace("__STACKCHAN_ROOT__", jsRoot) + "</head>");
        File.WriteAllText(Path.Combine(_stateDirectory, "desktop_widget.webview.html"), html, new UTF8Encoding(false));
    }

    private void WatchTrayStatus()
    {
        _statusWatcher = new FileSystemWatcher(_stateDirectory, "tray_status.json")
        {
            NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.Size | NotifyFilters.FileName,
            EnableRaisingEvents = true
        };
        _statusWatcher.Changed += (_, _) => ScheduleDashboardRefresh();
        _statusWatcher.Created += (_, _) => ScheduleDashboardRefresh();
        _statusWatcher.Renamed += (_, _) => ScheduleDashboardRefresh();
    }

    private void ScheduleDashboardRefresh()
    {
        Dispatcher.BeginInvoke(() =>
        {
            _dashboardRefreshTimer.Stop();
            _dashboardRefreshTimer.Start();
        });
    }

    private async Task RefreshDashboardAsync()
    {
        _dashboardRefreshTimer.Stop();
        try { await Browser.CoreWebView2.ExecuteScriptAsync("if(typeof refresh==='function'){refresh();}"); }
        catch { }
    }

    private void RestorePosition()
    {
        try
        {
            if (!File.Exists(_positionPath)) return;
            var saved = JsonSerializer.Deserialize<PointSnapshot>(File.ReadAllText(_positionPath));
            if (saved is null || !IsVisiblePosition(saved.Left, saved.Top)) return;
            Left = saved.Left;
            Top = saved.Top;
            WindowStartupLocation = WindowStartupLocation.Manual;
        }
        catch { }
    }

    private static bool IsVisiblePosition(double left, double top)
    {
        // SystemParameters uses the same WPF device-independent coordinate
        // system as Window.Left/Top, unlike WinForms Screen.WorkingArea.
        var desktop = new Rect(
            SystemParameters.VirtualScreenLeft,
            SystemParameters.VirtualScreenTop,
            SystemParameters.VirtualScreenWidth,
            SystemParameters.VirtualScreenHeight);
        return left + WidgetWidth > desktop.Left + 40 && left < desktop.Right - 40 && top + WidgetHeight > desktop.Top + 40 && top < desktop.Bottom - 40;
    }

    private void SchedulePositionSave()
    {
        _pendingPosition = new PointSnapshot(Left, Top);
        _positionSaveTimer.Stop();
        _positionSaveTimer.Start();
    }

    private void SavePosition()
    {
        _positionSaveTimer.Stop();
        var position = _pendingPosition ?? new PointSnapshot(Left, Top);
        try
        {
            var tmp = _positionPath + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(position), new UTF8Encoding(false));
            File.Move(tmp, _positionPath, true);
        }
        catch { }
    }

    [ComVisible(true)]
    public sealed class WidgetBridge
    {
        private readonly MainWindow _window;
        private readonly string _root;
        public WidgetBridge(MainWindow window, string root) { _window = window; _root = root; }
        public string ReadText(string path)
        {
            try
            {
                var stateRoot = Path.GetFullPath(Path.Combine(_root, "state")) + Path.DirectorySeparatorChar;
                var target = Path.GetFullPath(path);
                return target.StartsWith(stateRoot, StringComparison.OrdinalIgnoreCase) && File.Exists(target)
                    ? File.ReadAllText(target, new UTF8Encoding(false))
                    : string.Empty;
            }
            catch { return string.Empty; }
        }
        public int SetPin(bool enabled)
        {
            _window.Dispatcher.Invoke(() => _window.Topmost = enabled);
            return 0;
        }
        public void OpenSettings() => Process.Start(new ProcessStartInfo("notepad.exe", $"\"{Path.Combine(_root, "config.json")}\"") { UseShellExecute = true });
        public int RunPowerShell(string relativeScript, string args) => Run("powershell.exe", relativeScript, args);
        public int RunPwsh(string relativeScript, string args) => Run("pwsh.exe", relativeScript, args);
        public int RunPython(string relativeScript, string args) => Run(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "py.exe"), relativeScript, args, "-3");
        private int Run(string executable, string relativeScript, string args, string? prefix = null)
        {
            try
            {
                var script = Path.GetFullPath(Path.Combine(_root, relativeScript));
                var projectRoot = Path.GetFullPath(Path.Combine(_root, ".."));
                if (!script.StartsWith(projectRoot, StringComparison.OrdinalIgnoreCase) || !File.Exists(script)) return -1;
                var psi = new ProcessStartInfo(executable) { UseShellExecute = false, CreateNoWindow = true };
                if (!string.IsNullOrEmpty(prefix)) psi.ArgumentList.Add(prefix);
                if (!executable.EndsWith("py.exe", StringComparison.OrdinalIgnoreCase))
                {
                    psi.ArgumentList.Add("-NoProfile"); psi.ArgumentList.Add("-ExecutionPolicy"); psi.ArgumentList.Add("Bypass"); psi.ArgumentList.Add("-WindowStyle"); psi.ArgumentList.Add("Hidden"); psi.ArgumentList.Add("-File");
                }
                psi.ArgumentList.Add(script);
                foreach (var part in SplitArguments(args)) psi.ArgumentList.Add(part);
                Process.Start(psi);
                return 0;
            }
            catch { return -1; }
        }
        private static IEnumerable<string> SplitArguments(string text) => string.IsNullOrWhiteSpace(text) ? Array.Empty<string>() : text.Split(' ', StringSplitOptions.RemoveEmptyEntries);
    }

    private sealed record PointSnapshot(double Left, double Top);
}
