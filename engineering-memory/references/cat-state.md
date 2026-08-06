# CAT state machine

格式：`CAT:<state>:<ref_count>:<last_check>`，其中 state 为 `unobserved | observed | stable`。

- 新写入：`unobserved:0`。
- 在一个新任务中被实际使用一次：`observed:1`。
- 在至少两个不同任务中被实际使用且正文未变、无矛盾：`stable:2`；之后计数可继续增长。
- 同一任务重复使用只计一次。
- 只检索、只打开或仅出现在提示中不计引用。
- 正文或关键元数据的语义哈希变化：降为 `unobserved:0`。
- 发现冲突、路径失效、术语变更：降为 `unobserved:0` 并记录原因。
- `--dry-run` 必须保持所有文件逐字节不变。

CAT 标记只允许出现在正式记忆 frontmatter 和派生索引条目中。使用记录保存在 usage log；禁止在工程源文件中打标。
