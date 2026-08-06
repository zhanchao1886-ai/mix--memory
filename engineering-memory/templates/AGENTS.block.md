<!-- engineering-memory:start -->
## Engineering Memory global workflow

对所有工程任务无感启用 `$engineering-memory`：开始时先查 Skill 自带 `data/index.md`，不足时冷查且最多打开 3 篇；只把实际使用的记忆交给 `engineering_memory_cat`。任务收尾必须调用 `engineering_memory_recorder`，正式写入后由 `engineering_memory_indexer` 增量更新索引。

每次最终答复必须包含以“记忆备份：”开头的收据。短任务先备份不超过 300 token 的候选，并提示用户选择“锁定 / 不锁定 / 修改后锁定”。PreCompact 保存连续性胶囊，SessionStart(source=compact) 在同一任务中恢复；不要要求用户新建任务或重述已保存内容。达到 131,072 token 时 Stop 只落水位线和入队，后台 worker 整理并继续监测余量。流水线软阈值 25%、硬上限 30%；精准度优先。
<!-- engineering-memory:end -->
