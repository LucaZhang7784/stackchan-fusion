$ErrorActionPreference = "Stop"
$dir = "${STACKCHAN_ROOT}\fusion.firmware.0731\docker"
$task = "StackChan-HostExecutor"
schtasks /Create /TN $task /TR "powershell -NoProfile -ExecutionPolicy Bypass -File $dir\run_executor.ps1" /SC ONLOGON /F 2>&1
Write-Host "installed $task (runs at logon; watchdog every 5 min)"
schtasks /Create /TN "StackChan-HostExecutorWatchdog" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File $dir\run_executor.ps1" /SC MINUTE /MO 5 /F 2>&1
