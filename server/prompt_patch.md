# 提示词补丁: 让机器人唤醒后主动检查待播报消息

## 为什么需要
`fusion.robot_say` 排队的消息, 需要机器人在下次唤醒时由 LLM 调 `fusion.robot_pending` 取出来朗读。
只靠工具描述(description)有时不够, 加一条系统提示最稳妥。

## 步骤
1. 把容器内的默认提示词复制到 data 目录(主机路径 `D:\ProcessCenter\StackChan\server\data`):
   docker cp xiaozhi-esp32-server:/opt/xiaozhi-esp32-server/agent-base-prompt.txt D:\ProcessCenter\StackChan\server\data\.agent-base-prompt.txt
2. 编辑 `data\.agent-base-prompt.txt`, 在 <context> 内追加:

```
<fusion>
- 你有两个电脑端工具: codex_query(让电脑上的 Codex 执行任务)、claude_query(让电脑上的 Claude Code 执行任务)。用户说「让 Codex/Claude 做…」时调用, 并简要播报结果。
- 每次被唤醒后, 如果用户没有明确提问, 先调用 robot_pending 检查是否有待播报消息; 若有则逐条朗读, 朗读后再次调用 robot_pending(clear=true) 清除。
- 涉及连通性/机器人状态问题时, 调用 robot_status 查看分层检查结果。
</fusion>
```

3. 在 `data\.config.yaml` 增加一行(若没有 prompt_template):
   prompt_template: data/.agent-base-prompt.txt
4. 重启容器: docker compose -f D:\ProcessCenter\StackChan\server\docker-compose.yml restart xiaozhi-esp32-server