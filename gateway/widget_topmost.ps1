param(
  [ValidateSet('on','off')][string]$Mode,
  [int]$Width = 0,
  [int]$Height = 0
)
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class WidgetWindow {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string title);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int maxCount);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr insertAfter, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr LoadImage(IntPtr instance, string path, uint type, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, uint message, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool RedrawWindow(IntPtr hWnd, IntPtr updateRect, IntPtr updateRegion, uint flags);
  public static readonly IntPtr TOPMOST = new IntPtr(-1);
  public static readonly IntPtr NOTOPMOST = new IntPtr(-2);
}
'@
$hwnd = [WidgetWindow]::FindWindow($null, 'M5 StackChan')
$callback = [WidgetWindow+EnumWindowsProc]{
  param([IntPtr]$candidate, [IntPtr]$unused)
  $title = New-Object System.Text.StringBuilder 256
  [void][WidgetWindow]::GetWindowText($candidate, $title, $title.Capacity)
  if ($title.ToString() -eq 'M5 StackChan') {
    $script:hwnd = $candidate
    return $false
  }
  return $true
}
if ($hwnd -eq [IntPtr]::Zero) {
  [void][WidgetWindow]::EnumWindows($callback, [IntPtr]::Zero)
}
if ($hwnd -ne [IntPtr]::Zero) {
  $iconPath = Join-Path $PSScriptRoot 'assets\stackchan-app.ico'
  if (Test-Path -LiteralPath $iconPath) {
    # HTA 的 icon 属性会回退为默认图标；用原生窗口消息覆盖大小标题栏图标。
    # HTML Application Host 有一层无标题父窗；两层都设置才会更新标题栏图标。
    $iconTargets = @($hwnd, [WidgetWindow]::GetParent($hwnd))
    foreach ($target in $iconTargets) {
      if ($target -eq [IntPtr]::Zero) { continue }
      $small = [WidgetWindow]::LoadImage([IntPtr]::Zero, $iconPath, 1, 16, 16, 0x0010)
      $large = [WidgetWindow]::LoadImage([IntPtr]::Zero, $iconPath, 1, 32, 32, 0x0010)
      if ($null -ne $small -and $small.ToInt64() -ne 0) { [void][WidgetWindow]::SendMessage($target, 0x0080, [IntPtr]::Zero, $small) }
      if ($null -ne $large -and $large.ToInt64() -ne 0) { [void][WidgetWindow]::SendMessage($target, 0x0080, [IntPtr]::new(1), $large) }
      [void][WidgetWindow]::RedrawWindow($target, [IntPtr]::Zero, [IntPtr]::Zero, 0x0501)
    }
  }
  $flags = 0x0001 -bor 0x0010 # SWP_NOMOVE | SWP_NOACTIVATE
  if ($Width -le 0 -or $Height -le 0) { $flags = $flags -bor 0x0001 -bor 0x0002 } # keep existing size
  if ($Mode -eq 'on') {
    $after = [WidgetWindow]::TOPMOST
  } elseif ($Mode -eq 'off') {
    $after = [WidgetWindow]::NOTOPMOST
  } else {
    $after = [IntPtr]::Zero
    $flags = $flags -bor 0x0004 # SWP_NOZORDER
  }
  if (-not [WidgetWindow]::SetWindowPos($hwnd, $after, 0, 0, $Width, $Height, $flags)) {
    throw "无法更新 M5 StackChan 窗口层级"
  }
} else {
  throw "未找到 M5 StackChan 窗口"
}
