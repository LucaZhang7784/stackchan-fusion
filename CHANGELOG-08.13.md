# CHANGELOG v08.13（2026-08-13）

## 配网误触/误入根治（固件 reference/stackchan-xiaozhi-firmware-mqtt）

- **背景**：机器人反复出现"关机/重启后进入配网模式"。串口抓包定位到真凶：
  固件 `WifiBoard::TryWifiConnect` 启动 10 秒连接超时定时器，但 WifiStation
  组件要等 `IP_EVENT_STA_GOT_IP` 才上报 Connected（`WIFI_EVENT_STA_CONNECTED`
  处理器为空），board 才停掉定时器。家庭网络 DHCP 首个 OFFER 间歇性丢包/慢
  （实测 IP 到达 8~12 秒不等），10 秒窗口内未拿到 IP → 误判超时 → 自动进配网
  + 失败计数；旧固件连续 3 次失败还会**自动清空已存 SSID**，导致"每次开机必进
  配网"的恶性状态。
- **修复 1（连接超时 10s→20s）**：`wifi_board.cc` `CONNECT_TIMEOUT_SEC` 改为 20，
  覆盖 DHCP 首次 OFFER 丢包后的 lwIP 重试窗口（实测 8~12s 均落在窗口内），
  radio 已连上但 IP 未到不再被误杀。异地连不上时仍可开机单击屏幕立即进配网。
- **修复 2（进配网不再清空 SSID）**：`EnterWifiConfigMode` 移除
  `SsidManager::Clear()` —— 误入配网（开机单击/待机长按 5s）不再抹掉家庭 WiFi；
  配网成功经 `SsidManager::AddSsid` **追加**新网络，新旧 SSID 并存：
  异地配完网回家自动重连，去新地方也能照常进配网连其他 WiFi。
- **修复 3（移除 3 次失败自动清 SSID）**：`IncrementFailureCount` 不再累计失败后
  `Clear()`，偶发超时误进配网不再造成持久性丢配置。

## 真机验证（2026-08-13，串口连续复位）

- 修复前（10s 窗口）：复位 2/3 次出现 `WiFi connection timeout, entering config
  mode`，尽管 radio 已连上 DHSDWireless。
- 修复后（20s + 不清 SSID）：连续复位全部 `Got IP -> Connected -> idle`，零配网；
  IP 到达 8.3s / 11.4s，均落在窗口内。
- 保留行为：开机阶段单击触屏仍可进配网（供异地配网）；待机长按 5 秒仍可进配网。
