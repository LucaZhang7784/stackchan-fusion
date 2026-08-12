' Hidden launcher for run_bridge.ps1 (no console flash).
' Used by scheduled task StackChan-Bridge (logon auto-start).
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:/PROJECT_ROOT\fusion.firmware.0731\xiaozhi-mcp\run_bridge.ps1""", 0, False
