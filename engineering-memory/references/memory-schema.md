# Memory schema

正式记忆存放在 `data/projects/<project>/memories/<id>.md`。`project`、`id` 和所有路径段必须通过安全校验；索引的 `path` 相对 `data/`。

必需 frontmatter：

```yaml
id: "EM-20260806-ab12cd34"
title: "索引必须是派生产物"
project: "demo"
tags: ["memory", "index"]
category: "decision"
source: "task:task-id"
created: "2026-08-06T12:00:00+08:00"
updated: "2026-08-06T12:00:00+08:00"
cat: "CAT:unobserved:0:2026-08-06"
content_hash: "sha256:..."
```

类别只允许 `decision | outcome | lesson | glossary | filemap`。正文应是可独立理解的最小事实单元，包含结论、适用范围及必要证据，不保存聊天逐字稿。

禁止写入密码、令牌、Cookie、私钥、连接串；疑似密钥必须替换为 `[REDACTED]`。关键文件只写项目相对路径。无法安全相对化的路径写成文件名或职责描述，不保存主目录或盘符。

新记忆 `cat` 固定为 `CAT:unobserved:0:<当天日期>`。`content_hash` 只覆盖语义字段与正文，不覆盖 `cat`、`updated`，用于发现修改。
