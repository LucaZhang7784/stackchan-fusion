' Hidden launcher for fusion_tray.ps1 (no console flash).
' Used by scheduled task StackChan-FusionTray.
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\ProcessCenter\StackChan\fusion.firmware.0731\gateway\fusion_tray.ps1""", 0, False
