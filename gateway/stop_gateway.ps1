$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $root 'state\gateway.pid'
if (Test-Path -LiteralPath $pidFile) {
  $id = [int](Get-Content -LiteralPath $pidFile)
  try { Stop-Process -Id $id -Force -ErrorAction Stop; Write-Host "已停止网关 PID $id" }
  catch { Write-Host "进程 $id 已不存在" }
  Remove-Item -LiteralPath $pidFile -Force
} else {
  Write-Host '未找到 pid 文件'
  Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force; Write-Host "已停止占用 8010 的进程 $($_.OwningProcess)" }
}