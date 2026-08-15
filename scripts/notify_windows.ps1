# notify_windows.ps1 — Windows 系统通知(Toast)助手
# 由 fusion_gateway.py 在 agent 事件(完成/提问/待确认/报错)时后台调用:
#   powershell -File notify_windows.ps1 -Title "codex 任务完成" -Text "..."
# 优先现代 Toast(WinRT + AUMID 自注册); 失败回退系统托盘气泡。
param(
    [string]$Title = 'StackChan Fusion',
    [string]$Text = '',
    [string]$Tag = ''
)

$ErrorActionPreference = 'SilentlyContinue'
$AppId = 'StackChan.Fusion.Gateway'
$logFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'gateway\state\notify_windows.log'

function Write-Log([string]$m) {
    try {
        Add-Content -LiteralPath $logFile -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8
    } catch { }
}

function Set-Aumid {
    # 为 Start Menu 快捷方式写入 System.AppUserModel.ID, 使非打包应用可弹 Toast
    param([string]$ShortcutPath, [string]$AppId)
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class StackChanAppId {
    [DllImport("shell32.dll", CharSet=CharSet.Unicode)]
    static extern int SHGetPropertyStoreFromParsingName(string pszPath, IntPtr pbc, uint flags, ref Guid riid, out IntPtr ppv);
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        int GetCount(out uint cProps);
        int GetAt(uint iProp, out PK pkey);
        int GetValue(ref PK key, out PV pv);
        int SetValue(ref PK key, ref PV pv);
        int Commit();
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct PK { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Sequential)]
    public struct PV { public ushort vt; public ushort r1; public ushort r2; public ushort r3; public IntPtr p1; public IntPtr p2; }
    public static int Set(string path, string appId) {
        Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        IntPtr pv;
        int hr = SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 0, ref iid, out pv);
        if (hr != 0) return hr;
        IPropertyStore store = (IPropertyStore)Marshal.GetObjectForIUnknown(pv);
        PK key = new PK { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
        PV val = new PV { vt = 31, p1 = Marshal.StringToCoTaskMemUni(appId) };
        store.SetValue(ref key, ref val);
        store.Commit();
        Marshal.Release(pv);
        return 0;
    }
}
"@ -ErrorAction SilentlyContinue
    try { [StackChanAppId]::Set($ShortcutPath, $AppId) | Out-Null; return $true } catch { return $false }
}

# 注册 AUMID 快捷方式(一次性)
$lnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\StackChan Fusion.lnk'
try {
    if (-not (Test-Path -LiteralPath $lnk)) {
        $ws = New-Object -ComObject WScript.Shell
        $sc = $ws.CreateShortcut($lnk)
        $sc.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $sc.Arguments = '-NoProfile -WindowStyle Hidden -Command exit'
        $sc.Save()
        Set-Aumid $lnk $AppId | Out-Null
    }
} catch { }

$ok = $false
try {
    # 现代 Toast (WinRT)
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
    $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $textNodes = $xml.GetElementsByTagName('text')
    $textNodes.Item(0).AppendChild($xml.CreateTextNode($Title)) | Out-Null
    $textNodes.Item(1).AppendChild($xml.CreateTextNode($Text)) | Out-Null
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    if ($Tag) {
        try { $toast.Tag = $Tag; $toast.Group = 'stackchan' } catch { }
    }
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
    Start-Sleep -Seconds 5
    $ok = $true
} catch {
    Write-Log "toast 失败, 回退气泡: $($_.Exception.Message)"
}

if (-not $ok) {
    # 回退: 系统托盘气泡
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Information
        $n.Visible = $true
        $n.BalloonTipTitle = $Title
        $n.BalloonTipText = $Text
        $n.ShowBalloonTip(6000)
        Start-Sleep -Seconds 6
        $n.Visible = $false
        $n.Dispose()
    } catch { }
}

Write-Log "notify: $Title :: $($Text.Substring(0, [Math]::Min(80, $Text.Length)))"
