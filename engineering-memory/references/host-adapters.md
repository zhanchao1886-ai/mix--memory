# Host adapter protocol

核心只认识规范事件：`SessionStart | UserPromptSubmit | PreCompact | PostCompact | Stop | SessionEnd | SubagentStart | SubagentStop`。适配器只映射宿主字段，不改变记忆策略。

## Codex

安装器注册 `hooks/hooks.template.json`。PreCompact 使用 `trigger=manual|auto`；压缩后的 SessionStart 使用 `source=compact`，并在当前任务下一次模型请求前注入胶囊。Stop 只排队，不使用不受支持的 Hook `async`。

## WorkBuddy-like / generic JSON

公开桥接命令：

```bash
python3 scripts/hook_router.py handle --host workbuddy
```

stdin 最小契约：

```json
{
  "event": "before_compact | after_compact | task_resume | message_submitted | turn_stop",
  "event_id": "幂等键",
  "task_id": "宿主稳定任务ID",
  "project": "项目名",
  "prompt": "可选",
  "assistant_output": "可选",
  "transcript_path": "可选，只读 JSONL"
}
```

输出协议为 `engineering-memory.host-event.v1`，包含 `continue`、`decision`、`reason`、`additional_context` 与 `engineering_memory`。宿主必须在原 task ID 中消费 `additional_context`；若宿主只能创建新任务，则只能提供跨任务恢复，不能声称满足“无需新任务”。

Tencent WorkBuddy 与开源 work-buddy 的公开能力和扩展接口并不相同，本适配器不依赖任何私有 API。集成方只需把本机事件映射到上述稳定契约；没有 before/after compact 事件时，可在宿主压缩前主动调用 `context_continuity.py checkpoint`，恢复时调用 `resume`。
