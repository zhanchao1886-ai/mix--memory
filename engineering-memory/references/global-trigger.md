# Global trigger

Codex 无感触发由全局 `AGENTS.md` 和八类命令 Hook 协作：

- `SessionStart(startup|resume|compact)`：在有胶囊时恢复同一 task ID；compact 后立即注回。
- `UserPromptSubmit`：识别项目、记录轻量任务意图并提示热/冷检索。
- `PreCompact`：从胶囊和有界 transcript tail 捕获连续性 checkpoint。
- `PostCompact`：只确认胶囊就绪，不重复注入。
- `Stop`：快速落水位线、入队、启动 worker，并检查最终收据。
- `SessionEnd`：仅排队和审计；官方超时上限下不得做重活。
- `SubagentStart/Stop`：审计代理；记录代理缺收据时要求继续一次。

所有事件必须幂等；`stop_hook_active=true` 时不得再次阻止。Hook 或派生 worker 启动失败时 fail-open，工程任务本身仍可结束，队列保留供手动 drain。

Codex 当前只执行 command Hook，Hook 的 `async` 字段不会产生异步执行。因此后台能力由队列加独立 worker 实现，不能把 `async: true` 当作保证。

`install_global.py` 是唯一允许注册全局 AGENTS、Hook 和三代理的脚本。先 `--dry-run`，获得用户明确同意后才 `--apply`；用户还需在宿主中审阅并信任 Hook。
