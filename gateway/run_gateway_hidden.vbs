' Hidden launcher for run_gateway.ps1 (no console flash).
' Used by scheduled task StackChan-FusionGateway.
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\ProcessCenter\StackChan\fusion.firmware.0731\gateway\run_gateway.ps1""", 0, False
