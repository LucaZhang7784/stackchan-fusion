# M5 StackChan Widget

Chromium/WebView2 desktop-widget host for the existing StackChan dashboard.

Build with `dotnet publish -c Release -r win-x64 --self-contained false` after the .NET 8 SDK is installed. The executable is deliberately separate from the gateway so it can carry the robot icon rather than inheriting the PowerShell icon.
