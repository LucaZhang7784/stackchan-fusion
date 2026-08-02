$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$state = Join-Path $root 'state'
New-Item -ItemType Directory -Force -Path $state | Out-Null
$port = 8010
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) { Write-Host "网关已在运行 (PID $($listener[0].OwningProcess))"; exit 0 }
$py = (Get-Command python).Source
if (-not $py) { Write-Host '未找到 python'; exit 1 }
$p = Start-Process -FilePath $py -ArgumentList @('fusion_gateway.py','--transport','http','--host','0.0.0.0','--port',"$port") -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $state 'gateway.stdout.log') -RedirectStandardError (Join-Path $state 'gateway.stderr.log') -PassThru
Set-Content -LiteralPath (Join-Path $state 'gateway.pid') -Value $p.Id
Write-Host "网关已启动 PID $($p.Id), 端口 $port"