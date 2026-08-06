# Index and retrieval specification

`index.json` 是全量冷索引，`index.md` 是不超过 2,000 估算 token 的热索引；二者只能由脚本生成。条目字段统一为 `id/title/tags/category/project/cat/path`。

检索顺序：

1. 在热层按项目、精确 ID、标题、标签和关键词找候选。
2. 热层没有足够相关候选时查冷索引。
3. 相关性主排序，CAT 只在相关结果中加权：stable > observed > unobserved。
4. 最多返回 3 篇，只有确实影响任务的候选才标记为 used。

精准指标：

- 候选利用率 = 实际使用候选数 / 检索候选数，目标 ≥80%。没有候选时记为 `null`，不虚构 100%。
- 黄金集 Hit@3 = 已知相关记忆在前三名检索结果中的用例数 / 总用例数，目标 ≥80%。
- stable 占比与矛盾率写入使用日志，用于观测，不以牺牲相关性换 stable。

热层裁剪优先保留最近实际使用且 stable 的条目，再按 CAT、更新时间和 ID 稳定排序；完整条目始终保留在冷索引。
