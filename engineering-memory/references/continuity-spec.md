# Same-task continuity across compaction

连续性胶囊存放在 `data/projects/<project>/continuity/<task-id>.json`，只包含：目标、确认决策、开放问题、相对产物路径、下一步、实际使用的记忆 ID、最近少量压缩回合和水位线。

流程：

1. 用户回合和 Stop 只追加有界、已脱敏的 recent turn。
2. PreCompact 读取最多 1 MiB transcript tail，合并结构化胶囊并增加 `compression_count`。
3. PostCompact 不注回旧内容，避免重复上下文。
4. Codex 随后的 SessionStart(source=compact) 立即返回 ≤1,200 token 的恢复上下文；WorkBuddy 类宿主在 task_resume 时调用同一接口。
5. 恢复内容优先级：目标与已确认决策 > 开放问题与下一步 > 明确 memory IDs > 相关索引候选 > 最近回合。

连续性胶囊不等于正式记忆，也不声称增加模型单次上下文长度。它把被压缩的信息外部化并按需重注，从而允许同一 task ID 经历多轮压缩，而无需新建任务或要求用户重述。

显式 checkpoint 的质量高于自动 transcript 提取。关键任务应主动写入 `--decision`、`--open-loop`、`--artifact` 和 `--next-action`；所有 artifact 必须是相对路径。
