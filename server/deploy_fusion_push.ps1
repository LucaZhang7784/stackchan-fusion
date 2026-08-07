# 部署融合推送补丁: /api/push (服务器 TTS 直推设备) + SERVER_MCP 注册
# 用法: powershell -ExecutionPolicy Bypass -File deploy_fusion_push.ps1
param(
  [string]$StackChanRoot = '<PROJECT_ROOT>'
)
$ErrorActionPreference = 'Stop'
$fusion = Join-Path $StackChanRoot 'fusion.firmware.0731'
$composeBase = Join-Path $StackChanRoot 'server\docker-compose.yml'
$composeFusion = Join-Path $fusion 'server-patch\docker-compose.fusion.yml'
$dataDir = Join-Path $StackChanRoot 'server\data'
$configYaml = Join-Path $dataDir '.config.yaml'
$fusionSecret = 'YOUR_GATEWAY_TOKEN'

# 0) 确认补丁文件存在
foreach ($f in @("$fusion\server-patch\core\fusion_push.py", "$fusion\server-patch\core\connection.py", "$fusion\server-patch\core\http_server.py")) {
  if (-not (Test-Path -LiteralPath $f)) { throw "缺少 $f" }
}

# 1) 网关必须运行 (SERVER_MCP 注册依赖)
try { $health = (Invoke-WebRequest -Uri 'http://127.0.0.1:8010/healthz' -UseBasicParsing -TimeoutSec 5).StatusCode } catch { $health = 0 }
if ($health -ne 200) {
  Write-Host '网关未运行, 启动...'
  & (Join-Path $fusion 'gateway\run_gateway.ps1')
  Start-Sleep -Seconds 4
}

# 2) .config.yaml 写入 fusion_secret (带备份)
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
if (Test-Path -LiteralPath $configYaml) {
  $yaml = [System.IO.File]::ReadAllText($configYaml, [System.Text.Encoding]::UTF8)
  if ($yaml -notmatch 'fusion_secret') {
    Copy-Item -LiteralPath $configYaml -Destination (Join-Path $dataDir ".config.yaml.bak-$ts") -Force
    $yaml = [regex]::Replace($yaml, '(?m)^server:\s*$', "server:`n  fusion_secret: $fusionSecret")
    [System.IO.File]::WriteAllText($configYaml, $yaml, [System.Text.UTF8Encoding]::new($false))
    Write-Host "已写入 fusion_secret -> .config.yaml (备份 .config.yaml.bak-$ts)"
  } else {
    Write-Host 'fusion_secret 已存在, 跳过'
  }
}

# 3) 替换 SERVER_MCP 配置 (备份 + 覆盖)
$src = Join-Path $fusion 'server\.mcp_server_settings.json'
$dst = Join-Path $dataDir '.mcp_server_settings.json'
Copy-Item -LiteralPath $dst -Destination (Join-Path $dataDir ".mcp_server_settings.json.bak-$ts") -Force
Copy-Item -LiteralPath $src -Destination $dst -Force
Write-Host '已替换 .mcp_server_settings.json (SERVER_MCP -> fusion-gateway)'

# 4) 停止 -> 重建容器 (挂载补丁) -> 启动
Write-Host '停止并重建 xiaozhi-esp32-server (带融合补丁挂载)...'
docker compose -f $composeBase -f $composeFusion up -d --no-deps xiaozhi-esp32-server
Start-Sleep -Seconds 45

# 5) 验证
$ok = $true
try {
  $st = Invoke-RestMethod -Uri 'http://127.0.0.1:8003/api/status' -TimeoutSec 5
  Write-Host "PASS: /api/status ok=$($st.ok) 已注册连接: $($st.connections -join ', ')"
} catch {
  Write-Host "FAIL: /api/status 不可达: $($_.Exception.Message)"
  $ok = $false
}
$logs = docker logs --since 5m xiaozhi-esp32-server 2>&1 | Out-String
if ($logs -match '服务端MCP客户端已连接' -and ($logs -match 'fusion' -or $logs -match 'codex_query')) {
  $logs -split "`r?`n" | Select-String -Pattern '服务端MCP客户端已连接|可用工具' | Select-Object -Last 3 | ForEach-Object { Write-Host "PASS: $_" }
} else {
  Write-Host '注意: SERVER_MCP 注册在设备连接时才触发。请打开机器人电源等它重连, 然后运行:'
  Write-Host '  python <PROJECT_ROOT>\fusion.firmware.0731\scripts\verify_connectivity.py'
  Write-Host '网关与 /api/push 已就绪(/api/status 检查见上)。'
}
if (-not $ok) { Write-Host '部署未完全成功, 请检查后重试。'; exit 1 }
Write-Host '部署完成。机器人会重新连上服务器; 对机器人说「查一下Codex状态」验证端到端。'