' Hidden launcher for tray_watchdog.ps1 (no console flash).
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\ProcessCenter\StackChan\fusion.firmware.0731\gateway\tray_watchdog.ps1""", 0, False
