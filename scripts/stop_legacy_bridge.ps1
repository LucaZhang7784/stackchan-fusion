# 停止旧 bridge.js (已废弃, 与融合网关并存会重复注册 codex 工具)
param([switch]$Kill)
$procs = Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -like '*bridge.js*' }
if (-not $procs) { Write-Host '未发现旧 bridge.js 进程'; exit 0 }
foreach ($p in $procs) { Write-Host "PID $($p.ProcessId)  $($p.CommandLine.Substring(0, [Math]::Min(160, $p.CommandLine.Length)))" }
if ($Kill) {
  foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force; Write-Host "已停止 $($p.ProcessId)" }
  Write-Host '注意: 若 guard/计划任务在跑, 它可能把 bridge 拉起来; 需同时停 StackChanGuard 或相关计划任务。'
} else {
  Write-Host '加 -Kill 参数执行。'
}