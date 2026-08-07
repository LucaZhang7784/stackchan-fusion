' Hidden launcher for watchdog_funnel_proxy.ps1 (no console flash).
' Used by scheduled task StackChan-FunnelProxyWatchdog (every 5 min).
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""<PROJECT_DIR>\server\watchdog_funnel_proxy.ps1""", 0, False
