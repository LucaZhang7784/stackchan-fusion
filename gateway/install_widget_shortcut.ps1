param([string]$Desktop = [Environment]::GetFolderPath('Desktop'))
$root = Split-Path -Parent $PSCommandPath
$shortcutPath = Join-Path $Desktop 'M5 StackChan Widget.lnk'
$target = Join-Path $env:WINDIR 'System32\wscript.exe'
$launcher = Join-Path $root 'launch_stackchan_desktop_widget.vbs'
$icon = Join-Path $root 'assets\stackchan-app.ico'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcutPath)
$link.TargetPath = $target
$link.Arguments = '"' + $launcher + '"'
$link.WorkingDirectory = $root
$link.IconLocation = $icon + ',0'
$link.Description = 'M5 StackChan Desktop Widget'
$link.Save()
Write-Output $shortcutPath
