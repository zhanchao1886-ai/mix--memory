# Migration, upgrade and rollback

下载或迁移时整体复制 `engineering-memory/` 文件夹即可。`data/` 内不得出现绝对路径，脚本默认以自身所在目录解析相邻 `data/`。复制到含空格的新路径后，先运行：

```bash
python3 scripts/bootstrap_portable.py --host standalone
python3 scripts/bootstrap_portable.py --check-only
```

第一条初始化缺失的数据目录，第二条严格只读；对显式不存在的 `--root`，只读检查必须返回未就绪且不得创建目录。随后脚本应仍能记录、重建索引、检索、恢复胶囊和 drain 队列。

文件夹本身已经包含 Skill、脚本、配置和记忆库。Codex 全局无感生命周期 Hook 仍需在每个新宿主上执行一次注册与信任审阅：

```bash
python3 scripts/install_global.py --dry-run
python3 scripts/install_global.py --apply
```

升级代码时保留目标 `data/`，只替换 Skill 模板、参考和脚本；不覆盖无关的 `AGENTS.md`、hooks 或自定义代理。卸载只移除带工程记忆标记的区块、Hook 和三个代理文件，默认保留 `data/`。如要移除数据，必须由用户单独确认并手动备份。

WorkBuddy 类宿主不需要修改 Skill 内核，只需按 `host-adapters.md` 映射公开 JSON 事件；这不代表兼容任何未公开的厂商 API。

`data/` 可单独 Git 版本化；应忽略锁文件、`runtime/` 派生事件索引、`jobs/running/` lease 和临时文件，不忽略正式记忆、连续性胶囊、pending/done/failed job、索引、配置与日志。丢失 `runtime/` 后会从 append-only 日志自动重建。
