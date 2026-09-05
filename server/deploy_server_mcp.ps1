# 部署: 把融合网关注册进 xiaozhi-esp32-server 的 SERVER_MCP 通道
# 用法: powershell -ExecutionPolicy Bypass -File deploy_server_mcp.ps1 [-SkipRestart]
param(
  [string]$StackChanRoot = '${STACKCHAN_ROOT}',
  [switch]$SkipRestart
)
$ErrorActionPreference = 'Stop'
$fusion = Join-Path $StackChanRoot 'fusion.firmware.0731'
$src = Join-Path $fusion 'server\.mcp_server_settings.json'
$dataDir = Join-Path $StackChanRoot 'server\data'
$dst = Join-Path $dataDir '.mcp_server_settings.json'
if (-not (Test-Path -LiteralPath $src)) { throw "缺少 $src" }
# 1) 确保网关在跑
try { $health = (Invoke-WebRequest -Uri 'http://127.0.0.1:8010/healthz' -UseBasicParsing -TimeoutSec 5).StatusCode } catch { $health = 0 }
if ($health -ne 200) {
  Write-Host '网关未运行，先启动...'
  & (Join-Path $fusion 'gateway\run_gateway.ps1')
  Start-Sleep -Seconds 4
}
# 2) 备份并替换配置
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $dataDir ".mcp_server_settings.json.bak-$ts"
Copy-Item -LiteralPath $dst -Destination $backup -Force
Copy-Item -LiteralPath $src -Destination $dst -Force
Write-Host "已备份旧配置 -> $backup"
if ($SkipRestart) { Write-Host '已替换配置(未重启容器)'; exit 0 }
# 3) 重启容器
Write-Host '停止并重启 xiaozhi-esp32-server ...'
docker compose -f (Join-Path $StackChanRoot 'server\docker-compose.yml') restart xiaozhi-esp32-server
Start-Sleep -Seconds 40
# 4) 验证注册
$logs = docker logs --since 5m xiaozhi-esp32-server 2>&1 | Out-String
if ($logs -match '服务端MCP客户端已连接' -and ($logs -match 'fusion' -or $logs -match 'codex_query')) {
  Write-Host 'PASS: 融合工具已注册到 SERVER_MCP。'
  $logs -split "`r?`n" | Select-String -Pattern '服务端MCP客户端已连接|可用工具' | Select-Object -Last 3 | ForEach-Object { Write-Host $_ }
} else {
  Write-Host 'FAIL: 未检测到融合工具注册，回滚配置并重启...'
  Copy-Item -LiteralPath $backup -Destination $dst -Force
  docker compose -f (Join-Path $StackChanRoot 'server\docker-compose.yml') restart xiaozhi-esp32-server
  Write-Host '已回滚。请查看 gateway\state\gateway.stderr.log 与容器日志。'
  exit 1
}