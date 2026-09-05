param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSCommandPath
$logPath = Join-Path $root 'state\desktop_widget.log'
$existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessId -ne $PID -and $_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'stackchan_desktop_widget\.ps1'
} | Select-Object -First 1
if ($existing) {
    # One desktop widget is sufficient; leave the running instance intact.
    exit 0
}
$null = [System.IO.File]::WriteAllText($logPath, ("{0:o} native widget launch" -f (Get-Date)), [System.Text.UTF8Encoding]::new($false))
trap {
    [System.IO.File]::WriteAllText($logPath, ("{0:o} native widget start failed: {1}" -f (Get-Date), $_.Exception.ToString()), [System.Text.UTF8Encoding]::new($false))
    exit 1
}

# Native WPF shell for the existing Dash canvas.  The HTML remains the single
# source of truth for layout, status refresh, and button actions; WPF supplies
# a reliable Windows window, taskbar identity, and DPI-aware chrome.
Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName WindowsBase
Add-Type @'
using System;
using System.IO;
using System.Diagnostics;
using System.Runtime.InteropServices;

[ComVisible(true)]
public sealed class StackChanWidgetBridge {
  public string Root { get; private set; }
  public StackChanWidgetBridge(string root) { Root = root; }
  public string ReadText(string path) {
    try { return File.Exists(path) ? File.ReadAllText(path, new System.Text.UTF8Encoding(false)) : ""; }
    catch { return ""; }
  }
  public int Run(string relativeScript, string args, bool usePwsh) {
    try {
      var script = Path.GetFullPath(Path.Combine(Root, relativeScript));
      var exe = usePwsh ? "pwsh.exe" : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32\\WindowsPowerShell\\v1.0\\powershell.exe");
      var psi = new ProcessStartInfo(exe, "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + script + "\" " + (args ?? ""));
      psi.UseShellExecute = false; psi.CreateNoWindow = true;
      using (var p = Process.Start(psi)) { return p == null ? -1 : 0; }
    } catch { return -1; }
  }
  public int SetPin(bool enabled) {
    return Run("widget_topmost.ps1", "-Mode " + (enabled ? "on" : "off"), false);
  }
  public void OpenSettings() {
    try { Process.Start(new ProcessStartInfo("notepad.exe", "\"" + Path.Combine(Root, "config.json") + "\"")); } catch {}
  }
}
'@

$dash = Join-Path $root 'M5 StackChan Desktop Widget.hta'
$icon = Join-Path $root 'assets\stackchan-app.ico'
if (-not (Test-Path -LiteralPath $dash)) { throw "未找到 Dashboard: $dash" }

$window = New-Object System.Windows.Window
$window.Title = 'M5 StackChan'
$window.Width = 976
$window.Height = 676
$window.MinWidth = 976
$window.MaxWidth = 976
$window.MinHeight = 676
$window.MaxHeight = 676
$window.ResizeMode = 'NoResize'
$window.WindowStartupLocation = 'CenterScreen'
$window.Background = [System.Windows.Media.Brushes]::Black
$window.ShowInTaskbar = $true
if (Test-Path -LiteralPath $icon) {
    $window.Icon = New-Object System.Windows.Media.Imaging.BitmapImage([Uri]$icon)
}

$positionPath = Join-Path $root 'state\desktop_widget_position.json'
if (Test-Path -LiteralPath $positionPath) {
    try {
        $saved = Get-Content -LiteralPath $positionPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -ne $saved.left -and $null -ne $saved.top) {
            $window.Left = [double]$saved.left
            $window.Top = [double]$saved.top
            $window.WindowStartupLocation = 'Manual'
        }
    } catch {
        # Ignore an incomplete position snapshot and use the normal center.
    }
}
$window.add_LocationChanged({
    try {
        $position = @{ left = [double]$window.Left; top = [double]$window.Top } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($positionPath, $position, [System.Text.UTF8Encoding]::new($false))
    } catch {}
})

$browser = New-Object System.Windows.Controls.WebBrowser
$window.Content = $browser
$bridge = New-Object StackChanWidgetBridge($root)
$browser.ObjectForScripting = $bridge

# The existing canvas compensates for the legacy HTA host's 125% scaling.
# The WPF WebBrowser reports real client pixels, so use a 1:1 host scale.
$browser.add_LoadCompleted({
    try {
        $null = $browser.InvokeScript('execScript', @('hostScale=1; fit();'))
    } catch {
        # The dashboard itself has an automatic refresh and remains usable if
        # a legacy engine blocks this optional visual calibration.
    }
})

$html = Get-Content -LiteralPath $dash -Raw -Encoding UTF8
$html = [regex]::Replace($html, '(?im)^<hta:application\b.*?/>\s*', '')
$baseUri = ([Uri]$root).AbsoluteUri.TrimEnd('/') + '/'
$html = $html -replace '<head>', ("<head><base href=""{0}"">" -f $baseUri)
$jsRoot = $root.Replace('\', '\\')
$html = [regex]::Replace(
    $html,
    'var widgetPath=.*?var statusPath=root\+\x27\\\\state\\\\tray_status\.json\x27;',
    ("var root='{0}';var statusPath=root+'\\state\\tray_status.json';" -f $jsRoot)
)
$html = $html -replace 'hostScale=1\.25', 'hostScale=1'
# The WPF host is fixed at 976 x 696 physical pixels.  A deterministic scale
# avoids the legacy WebBrowser reporting a zero client width during onload.
$html = $html -replace '</head>', '<style>html,body{-ms-user-select:none;user-select:none}</style><script language="JScript">function readText(path){try{return window.external.ReadText(path)||"";}catch(e){return "";}}function runPs(file,args,visible){return window.external.Run(file,args||"",false)>=0;}function runPwsh(file,args){return window.external.Run(file,args||"",true)>=0;}function runPy(file,args){return window.external.Run(file,args||"",false)>=0;}function openSettings(){window.external.OpenSettings();}function setPin(v){var code=window.external.SetPin(!!v);if(code<0){alert("置顶操作未完成，请重试。");return;}pinned=!!v;var b=document.getElementById("pin");b.className=pinned?"toggle":"toggle off";b.title=pinned?"窗口始终置顶：开":"窗口始终置顶：关";}function fit(){document.getElementById("ui").style.transform="scale(0.63)";}function lockSize(){fit();}document.onselectstart=function(){return false;};</script></head>'
$nativeDash = Join-Path $root 'state\desktop_widget.native.html'
[System.IO.File]::WriteAllText($nativeDash, $html, [System.Text.UTF8Encoding]::new($false))
$browser.Navigate([Uri]$nativeDash)
[void]$window.ShowDialog()
