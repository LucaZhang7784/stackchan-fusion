# Phase 7 规划 (2026-08-05)

## 一、公开项目调研结论

| 项目 | 要点 | 对我们的启发 |
|---|---|---|
| [m5stack/StackChan](https://github.com/m5stack/StackChan) (官方) | 社区共创开源, 基于 xiaozhi 固件栈, 有 StackChan World 分享平台 | 官方生态持续更新, 值得跟踪其固件/表情资产 |
| [rt-net/stack-chan](https://github.com/rt-net/stack-chan) | 原始 Stack-chan (JS), RT 版 | 表情/动作灵感来源 |
| [rudyll/stackchan_ha_addons](https://github.com/rudyll/stackchan_ha_addons) | **Home Assistant 替代 xiaozhi 云, 可切换 OpenAI Realtime / Gemini Live 实时语音 + HA 设备控制** | **最相关**: 端到端实时语音(免 ASR+LLM+TTS 三段), 证明自建+实时语音可行 |
| [CaddonThaw/xiaozhi-esp32](https://github.com/CaddonThaw/xiaozhi-esp32) 等 MCP 系 | xiaozhi-esp32 的 MCP 版聊天机器人 | MCP 已成为小智生态主流扩展方式, 我们的服务端 MCP 方向正确 |
| [uncle-mark/desk-emoji](https://github.com/uncle-mark/desk-emoji) | 开源桌面机器人, 表情屏 + 2 轴云台 + LLM 语音 | 低成本替代硬件方案参考 |
| [techedger/esp_sparkbot](https://github.com/techedger/esp_sparkbot) | ESP32-S3 语音/图像机器人 | 语音+视觉一体参考 |
| otto 桌面人形机器人生态 | xiaozhi 1.7 + MCP 控制机器人动作 | MCP 控制动作已是标准做法 |

## 二、Phase 7 目标: 从「能用」到「好用」

### P7-1 实时语音链路 (最高优先)
- **✅ 已完成 (2026-08-05)**: 接入 **Qwen-Audio-3.0-Realtime** (阿里百炼)。
  端到端语音, 语义 VAD(smart_turn) 自动判停, 支持打断与 Function Calling;
  屏幕双向显示用户转写与回复文本。详见 `docs/p7-qwen-realtime.md`。
- 备选: 豆包实时语音 3.0 (邀测)、讯飞超拟人 (传统 SDK); OpenAI/Gemini
  需海外 key + 网络 (本机 WARP 可满足网络前提)。
- 预期达成: 端到端延迟 ~1-2s, 天然支持语音打断。

### P7-2 链路稳定性
- Funnel 抖动治理: 增加「直连 Tailscale 节点」备线(同 tailnet 时), Funnel 故障自动切换;
  或本机局域网直连(when AP 隔离允许)。
- 连接心跳: 服务器空闲期定时发保活帧, 减少半开连接。
- 主动播报失败重试限次 + 去重(已有队列清理, 补送达确认)。

### P7-3 智能体生态
- 扩展 agent 类型: 接入更多 CLI/服务(如 Gemini CLI、Aider), 或通过 MCP 市场挂第三方工具。
- agent_query 支持「多步任务编排」(如: 先查状态→再执行→再汇总)。
- 语音确认回环扩展到 codex/agy/pi (目前仅 claude 完整)。

### P7-4 感知与交互
- 人脸识别 + 视线追踪(利用既有摄像头 + BMI270 姿态), 说话人转向。
- 情绪化表情/动作联动(官方 StackChan 资产)。
- 视觉问答增强: 定时「看你一眼」+ 主动描述场景。

### P7-5 桌面/家居集成
- Home Assistant 控制(参考 stackchan_ha_addons): 语音控制灯光/空调等。
- 日程/提醒/邮件通知源 → 主动播报。

### P7-6 多机/部署
- 网关支持多台机器人(MAC 路由), 一套服务器管多台 StackChan。
- 一键部署脚本: `install.ps1` 自动装 docker/依赖/计划任务(已有雏形, 补全)。

## 三、风险与权衡

| 风险 | 说明 | 缓解 |
|---|---|---|
| 实时语音 API 费用 | OpenAI Realtime / Gemini Live 按分钟计费 | 保留免费 paraformer 链路, 按需切换 |
| 实时语音稳定性 | 依赖公网/API SLA | 双链路自动降级 |
| Funnel 公网抖动 | 不可控 | P7-2 备线 + 心跳 |
| 固件改动回归 | 每次刷机有风险 | 保留 07.31 回退包 + 分步验证 |
| 范围过大 | 6 个子项全做易烂尾 | 按 P7-1→P7-2→P7-5 优先级推进, 每步可交付 |

## 四、交付后距离最终需求

**最终需求**: 机器人成为桌面「语音中控」— 随时对话、主动播报 agent 动态、
语音指挥所有工具、多机可控、稳定低延迟。

- 完成 P7-1~P7-5 后: 覆盖 **90%**, 剩余是打磨(识别率、表情、生态)。
- **需要 Phase 8 吗?** 若 P7 只做 P7-1+P7-2(实时语音+稳定性), 交付「对话极快且稳」,
  不需要 Phase 8; 若要全做(含 HA/多机), 建议拆 Phase 8 收尾 HA + 社区分享。

## 五、建议 Phase 7 第一刀

**先做 P7-1 实时语音的最小验证**: 用 stackchan_ha_addons 的方式, 在服务器加一个
OpenAI Realtime 通道(固件不变, 服务器协议层切换), 实测端到端延迟与打断效果。
通过后再决定是否保留/扩展。
