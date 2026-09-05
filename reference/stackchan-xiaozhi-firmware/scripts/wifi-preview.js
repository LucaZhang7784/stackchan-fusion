#!/usr/bin/env node
/**
 * StackChan WiFi 配网页面预览 (手机模拟)
 * Usage: node scripts/wifi-preview.js
 * 修改 HTML 后刷新浏览器即可，无需编译烧录
 */

const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = 3000;
const HTML_DIR = path.join(__dirname, "..", "managed_components/78__esp-wifi-connect/assets");

const MOCK_SCAN = {
  support_5g: false,
  aps: [
    { ssid: "MyWiFi_2.4G", rssi: -42, authmode: 3 },
    { ssid: "Neighbor_Net", rssi: -58, authmode: 3 },
    { ssid: "CoffeeShop_Free", rssi: -71, authmode: 0 },
    { ssid: "Office_Guest", rssi: -65, authmode: 3 },
    { ssid: "Home_5G", rssi: -80, authmode: 3 },
  ],
};
const MOCK_SAVED = ["MyWiFi_2.4G", "Old_Router"];
const MOCK_CONFIG = {
  ota_url: "",
  max_tx_power: 80,
  remember_bssid: true,
  sleep_mode: true,
  show_ota_config: true,
  show_sleep_config: true,
};

function api(res, data) {
  res.writeHead(200, { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" });
  res.end(JSON.stringify(data));
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");

  if (req.method === "OPTIONS") {
    res.writeHead(204, { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,OPTIONS", "Access-Control-Allow-Headers": "Content-Type" });
    return res.end();
  }

  // API mocks
  if (url.pathname === "/scan") return api(res, MOCK_SCAN);
  if (url.pathname === "/saved/list") return api(res, MOCK_SAVED);
  if (url.pathname === "/advanced/config") return api(res, MOCK_CONFIG);
  if (url.pathname === "/advanced/submit" || url.pathname === "/submit" ||
      url.pathname.startsWith("/saved/delete") || url.pathname.startsWith("/saved/set_default")) {
    return api(res, { success: true });
  }

  // Serve static files
  let filePath = path.join(HTML_DIR, url.pathname === "/" ? "wifi_configuration.html" : url.pathname);
  const ext = path.extname(filePath);
  const mimes = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
                  ".png": "image/png", ".svg": "image/svg+xml" };

  fs.readFile(filePath, (err, data) => {
    if (err) { res.writeHead(404); return res.end("Not found"); }
    res.writeHead(200, { "Content-Type": mimes[ext] || "text/plain", "Access-Control-Allow-Origin": "*" });
    res.end(data);
  });
});

server.listen(PORT, () => {
  console.log(`\n  配网预览 http://localhost:${PORT}\n`);
});
