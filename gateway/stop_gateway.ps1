$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $root 'state\gateway.pid'
if (Test-Path -LiteralPath $pidFile) {
  $id = [int](Get-Content -LiteralPath $pidFile)
  try { Stop-Process -Id $id -Force -ErrorAction Stop; Write-Host "已停止网关 PID $id" }
  catch { Write-Host "进程 $id 已不存在" }
  Remove-Item -LiteralPath $pidFile -Force
} else {
  Write-Host '未找到 pid 文件'
}
# 兜底(2026-08-15): 无论 pid 文件是否过期, 一律按 8010 端口停止真实网关进程,
# 保证托盘「重启激活所有服务」可靠生效。
Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
  Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
  Write-Host "已停止占用 8010 的进程 $($_.OwningProcess)"
}
