#!/usr/bin/env node
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

// ===== 配置 =====
// Codex 侧 MCP 服务器: 提供 stackchan_check_task / stackchan_respond。
// respond 直接写入 fusion 网关的待播报队列(pending.jsonl), 由网关 _push_loop
// 走 MQTT(µ-law)推给机器人播报 —— 与当前云链路架构一致, 不再依赖旧 19783 bridge。
const BRIDGE_DIR = path.join(__dirname, '..', '.codex-bridge-broker');
const PENDING_TASK = path.join(BRIDGE_DIR, 'pending_task.json');
const OUTBOX = path.join(BRIDGE_DIR, 'outbox.json');
const GATEWAY_PENDING = path.join(__dirname, '..', 'fusion.firmware.0731', 'gateway', 'state', 'pending.jsonl');

function ensureDir() {
  if (!fs.existsSync(BRIDGE_DIR)) fs.mkdirSync(BRIDGE_DIR, { recursive: true });
}

function readJSON(fp, def) {
  try {
    if (!fs.existsSync(fp)) return def;
    let raw = fs.readFileSync(fp, 'utf-8');
    if (raw.charCodeAt(0) === 0xFEFF) raw = raw.slice(1);
    raw = raw.trim();
    if (!raw) return def;
    return JSON.parse(raw);
  } catch (e) { return def; }
}

function writeJSON(fp, data) {
  fs.writeFileSync(fp, JSON.stringify(data, null, 2), 'utf-8');
}

function nowIso() {
  const d = new Date();
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? '+' : '-';
  const pad = (n) => String(n).padStart(2, '0');
  const tz = sign + pad(Math.floor(Math.abs(off) / 60)) + ':' + pad(Math.abs(off) % 60);
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + 'T' +
         pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) + tz;
}

// ===== 入队到网关待播报队列 =====
function enqueueGateway(text) {
  try {
    fs.mkdirSync(path.dirname(GATEWAY_PENDING), { recursive: true });
    const entry = {
      id: crypto.randomBytes(4).toString('hex'),
      text: String(text || '').slice(0, 500),
      source: 'codex-bridge',
      created_at: nowIso()
    };
    fs.appendFileSync(GATEWAY_PENDING, JSON.stringify(entry) + '\n', 'utf-8');
    return true;
  } catch (e) {
    try { fs.appendFileSync(path.join(__dirname, 'bridge.log'), '[' + new Date().toISOString() + '] enqueue err: ' + e.message + '\n'); } catch (_) {}
    return false;
  }
}

// ===== MCP 工具定义 =====
const TOOLS = [
  {
    name: 'stackchan_check_task',
    description: '检查StackChan机器人是否发来了新指令。每次调用时检查 pending_task.json，如果有新的待处理任务则返回任务内容，没有则返回空。建议在每轮对话开始时调用。',
    inputSchema: { type: 'object', properties: {}, required: [] }
  },
  {
    name: 'stackchan_respond',
    description: '将处理结果发送给StackChan机器人播报。处理完机器人发来的指令后调用此工具，结果会进入网关待播报队列并推送给机器人语音播报。',
    inputSchema: { type: 'object', properties: { content: { type: 'string', description: '要播报的结果内容' } }, required: ['content'] }
  }
];

// ===== MCP 协议处理 =====
function handleMessage(msg) {
  const method = msg.method;
  const id = msg.id;

  if (method === 'initialize') {
    return { jsonrpc: '2.0', id: id, result: {
      protocolVersion: '2024-11-05',
      capabilities: { tools: {} },
      serverInfo: { name: 'stackchan-bridge', version: '2.0.0' }
    }};
  }

  if (method === 'notifications/initialized' || method === 'notifications/cancelled') {
    return null;
  }

  if (method === 'tools/list') {
    return { jsonrpc: '2.0', id: id, result: { tools: TOOLS } };
  }

  if (method === 'tools/call') {
    const toolName = msg.params && msg.params.name;
    const args = (msg.params && msg.params.arguments) || {};

    if (toolName === 'stackchan_check_task') {
      ensureDir();
      const pt = readJSON(PENDING_TASK, {});
      if (pt.task && pt.status === 'pending') {
        pt.status = 'processing';
        pt.processingAt = new Date().toISOString();
        writeJSON(PENDING_TASK, pt);
        return { jsonrpc: '2.0', id: id, result: {
          content: [{ type: 'text', text: JSON.stringify({ task: pt.task, id: pt.id, timestamp: pt.timestamp }) }]
        }};
      }
      return { jsonrpc: '2.0', id: id, result: { content: [{ type: 'text', text: '' }] } };
    }

    if (toolName === 'stackchan_respond') {
      const content = args.content || '';
      const ok = enqueueGateway(content);
      ensureDir();
      const ob = readJSON(OUTBOX, { messages: [] });
      ob.messages.push({ id: Date.now().toString(36), content: content, timestamp: new Date().toISOString(), source: 'codex', queued: ok });
      writeJSON(OUTBOX, ob);
      const pt = readJSON(PENDING_TASK, {});
      pt.status = 'done';
      pt.completedAt = new Date().toISOString();
      writeJSON(PENDING_TASK, pt);
      return { jsonrpc: '2.0', id: id, result: {
        content: [{ type: 'text', text: ok ? '已进入播报队列，机器人即将播报' : '入队失败，请检查网关' }]
      }};
    }

    return { jsonrpc: '2.0', id: id, result: { content: [{ type: 'text', text: '未知工具' }] } };
  }

  if (method === 'ping') {
    return { jsonrpc: '2.0', id: id, result: {} };
  }

  if (id !== undefined && id !== null) {
    return { jsonrpc: '2.0', id: id, result: {} };
  }
  return null;
}

// ===== stdio 主循环 =====
let buf = '';
process.stdin.setEncoding('utf-8');
process.stdin.on('data', function (chunk) {
  buf += chunk;
  let idx;
  while ((idx = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, idx).trim();
    buf = buf.slice(idx + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch (e) { continue; }
    const resp = handleMessage(msg);
    if (resp) {
      process.stdout.write(JSON.stringify(resp) + '\n');
    }
  }
});
process.stdin.on('end', function () { process.exit(0); });
