# Usage log, budget and receipt

`data/usage-log.jsonl` 是仅追加事件流。项目事件至少包含 `event_id/event/time/project/task_id`，并可带 `seq/activity_tokens`；事件 ID 用于防重复。候选备份、检索、实际使用、冲突、预算、连续性 checkpoint、Hook、队列与整理都写事件。不得用全文件重写实现追加。

`data/runtime/event-ids.jsonl` 与 `event-index.json` 是可删除、可重建的幂等加速层：它们只索引事件 ID 和日志字节状态，不是事实来源，也不随 Git 迁移。日志被移动、截断或修改后必须自动重建；日志存在非法 JSON 行时应失败并报告，不得静默跳过。

预算口径：

```text
pipeline_ratio = memory_pipeline_tokens / final_task_total_tokens
```

软阈值 25%，硬上限 30%。到达软阈值后，限制候选扩展、跳过非必要复核并禁止前台全量索引；硬上限时只保留必要检索、用户明确要求的锁定和最终收据。若总 token 未知，记录 `measurement=estimated`，不得伪装为精确值。

正式记忆文件表示“已锁定”。短任务候选只存在日志中，状态为 `deferred`；用户拒绝后状态为 `rejected`，永不自动晋升。用户锁定或 128K 整理晋升后状态为 `locked`。

每个任务收据必须说明：锁定/候选数量、索引状态、CAT 变化、预算、128K 当前值与是否到期。记录代理自身结束也必须输出该描述；未锁定候选要追加三选一提示：锁定、不锁定、修改后锁定。
