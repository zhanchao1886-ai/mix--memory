---
name: engineering-memory
description: 全局、无感、可迁移地检索、记录、校验并延续工程项目记忆。适用于任何工程任务的开始、执行、收尾、上下文压缩与同任务恢复；当用户提到历史决策、踩坑、术语、关键文件、记住这个、记忆锁定、128K 整理、跨设备复制、压缩后继续、无需新建任务、Codex Hook、WorkBuddy 或其他持久任务宿主时必须使用。维护记录代理、索引代理、薛定谔猫代理，以及热索引—冷文档—CAT 稳定性和连续性胶囊。
---

# Engineering Memory

## 目标与边界

用 Skill 文件夹内的外部记忆延续工程任务，精准度优先，并把前台记忆流水线控制在任务总 token 的 30% 以内。连续性胶囊能跨越多次上下文压缩继续同一任务，但不改变模型单次请求的物理上下文上限。

普通运行脚本只能写本 Skill 的 `data/`。所有持久路径必须相对 `data/`；不得保存用户目录、盘符、仓库绝对路径或秘密。除 `install_global.py --apply` 外，不得改动宿主配置。

下载完整文件夹后先运行：

```bash
python3 scripts/bootstrap_portable.py --host standalone
```

该命令仅初始化并自检 Skill 内部数据。Codex 全生命周期触发需一次注册；其他宿主按公开 JSON 协议映射事件。见 `references/host-adapters.md`。

## 五步流程

### 1. 识别项目并检索

从 Git 根目录名或当前目录名识别项目，不保存绝对根路径。先读 `data/index.md`；不足时冷查：

```bash
python3 scripts/search_memory.py "关键词" --project "项目名" --limit 3 \
  --log-usage --task-id "$TASK_ID"
```

最多打开 3 篇。相关性主排序，CAT 只作受限加权；stable 不能覆盖更相关结果。

### 2. 执行并标记实际使用

只有候选确实影响决策、命令或产出才算使用：

```bash
python3 scripts/mark_memory.py --project "项目名" --task-id "$TASK_ID" --used EM-...
```

只检索或打开不增加 CAT。正文修改、路径失效或矛盾时使用 `--conflict EM-... --reason "原因"` 降回 unobserved。详见 `references/cat-state.md`。

### 3. 维护同任务连续性

重要决策、开放问题、产物与下一步可显式写入胶囊：

```bash
python3 scripts/context_continuity.py checkpoint --project "项目名" --task-id "$TASK_ID" \
  --goal "目标" --decision "已确认决策" --open-loop "待解决" --next-action "下一步"
```

在支持生命周期事件的宿主中：PreCompact 落盘胶囊，PostCompact 只确认完成，SessionStart(source=compact) 将 ≤1,200 token 的目标、决策、开放问题、最近回合及最多 3 个记忆引用注回同一任务。禁止把完整旧对话重新塞入上下文。

### 4. 记录工程记忆并收尾

记录代理只保存 `decision | outcome | lesson | glossary | filemap`。明确说“记住这个”或形成耐久结论时锁定；普通短任务先写 ≤300 token 候选：

```bash
python3 scripts/record_memory.py --project "项目名" --title "标题" \
  --category lesson --content "内容" --mode candidate --task-id "$TASK_ID"
```

候选后询问：`锁定 / 不锁定 / 修改后锁定`。正式记忆从 `CAT:unobserved:0:<date>` 开始，并由索引代理调用 `index_memory.py --changed-id EM-...`，禁止手改索引。

任务结束记录预算并 finalize：

```bash
python3 scripts/mark_memory.py --project "项目名" --task-id "$TASK_ID" \
  --pipeline-tokens 1200 --task-total-tokens 6000 --finalize
```

25% 起进入 guarded，超过 30% 进入 minimal；短任务禁止全库重建。

### 5. Stop 快速落水位线，后台整理

Stop 只执行：更新连续性胶囊、记录水位线、幂等入队并启动独立 worker。不得在 Stop 内直接重建全库。项目未整理活动达到 122,880 token 标记 soon，达到 131,072 标记 due；后台运行：

```bash
python3 scripts/maintenance_worker.py --drain
```

worker 按水位线晋升未拒绝候选、重建索引；成功后仅扣除已处理区间，保留 token offset 和新到内容继续监测。失败不推进 checkpoint，并按上限重试。

最终答复必须包含：

```text
记忆备份：已锁定 <n> 条 / 候选 <n> 条 / 未产生；索引：已更新 / 无需更新；
CAT：<变化或无变化>；预算：<pipeline>/<total>=<ratio>；128K：<值与状态>。
```

记录代理缺少该描述时，SubagentStop 要求其继续；主任务缺收据时，Stop 只阻止一次，`stop_hook_active=true` 时放行以避免循环。

## 三代理与模块边界

- 记录代理：提炼五类事实、处理锁定选择、输出收据；禁止复制完整聊天。
- 索引代理：只通过脚本维护热层和冷层；禁止手工修索引。
- 薛定谔猫代理：按不同任务实际使用、语义哈希和冲突证据维护 CAT。
- 连续性模块：只保存同一任务的最小恢复状态，不替代正式记忆。
- 后台模块：只消费持久队列，不参与前台语义判断。
- 宿主适配器：只转换事件字段，不实现记忆策略。

完整模块图和测试边界见 `references/module-map.md`。

## 按需参考

- 字段与安全：`references/memory-schema.md`
- 索引与精准指标：`references/index-spec.md`
- CAT 状态机：`references/cat-state.md`
- 日志、预算、收据：`references/usage-log-spec.md`
- 后台 128K 整理：`references/consolidation-spec.md`
- 压缩与同任务恢复：`references/continuity-spec.md`
- Codex / WorkBuddy 类适配：`references/host-adapters.md`
- 模块拆分与测试：`references/module-map.md`
- 注册与 Hook：`references/global-trigger.md`
- 迁移、升级、回滚：`references/migration-upgrade.md`
