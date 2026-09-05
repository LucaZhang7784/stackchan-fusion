$root = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $root 'stop_gateway.ps1')
& (Join-Path $root 'run_gateway.ps1')
