# Background 128K consolidation

128K 指项目自己的尚未整理活动 token，不是模型上下文窗口。

- 122,880：`maintenance_due=soon`。
- 131,072：`maintenance_due=due`。
- Stop 只写胶囊和当前 `last_event_seq` 水位线，向 `data/jobs/pending/` 幂等入队，再启动独立 worker；禁止前台执行整理。
- `maintenance_worker.py` 原子认领任务，同一 job 只有一个 worker 能处理。
- running job 带 `claim_token` 和默认 900 秒 lease；worker 崩溃后过期 job 回到 pending，旧 worker 即使恢复也不得覆盖新 claim 的结果。
- worker 每次最多处理配置的 chunk 数；成功后推进完整事件序号和部分事件 token offset。
- worker 只处理 job 记录的 Stop `watermark_seq`；整理期间的新事件以及未消费 offset 留在 `unconsolidated_tokens`，继续监测，即使 chunk 容量大于 128K 也不得越线。
- 失败不推进 checkpoint；低于 `max_attempts` 时重新入队，最终失败进入 `jobs/failed/`。
- 错误文本先移除秘密和绝对路径；完成或最终失败后清理项目的 active job 指针。
- rejected 候选永不晋升；deferred 候选只能在水位线覆盖后晋升。

维护 token 单独记账，不计入触发它的前台任务 30% 流水线预算。宿主不允许派生进程时，把 `background.mode` 设为 `manual`，由宿主调度 `maintenance_worker.py --drain`。
