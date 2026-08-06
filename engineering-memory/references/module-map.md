# Module map and test boundaries

| Module | Responsibility | Persistent writes | Primary tests |
|---|---|---|---|
| `_memory_common.py` | path safety, atomic I/O, append-only event log, derived id index, locking, schema, token estimation | data root only | core, security, Stop latency |
| `record_memory.py` | candidate/locked/rejected lifecycle and receipt | memories, usage log | core |
| `index_memory.py` | derived hot and cold indexes | index files | core, migration |
| `search_memory.py` | relevance-first hot/cold retrieval | optional usage event | precision |
| `mark_memory.py` | CAT, budget, metrics, finalize | memory metadata, log | CAT, budget |
| `context_continuity.py` | compact capsule capture and same-task restoration | continuity JSON, log | compaction, transcript |
| `maintenance_queue.py` | idempotent enqueue, lease recovery, claim fencing, retry, detach | jobs, state, log | concurrency, crash, redaction |
| `maintenance_worker.py` | consume jobs outside Stop | job/result data | background integration |
| `consolidate_project.py` | watermark processing and candidate promotion | memories, index, state | 128K remainder |
| `adapters/*` | host event normalization/output rendering | none | protocol contract |
| `hook_router.py` | lifecycle orchestration and receipt gate | through modules | hooks, latency, continuity |
| `bootstrap_portable.py` | downloaded-folder initialization and strict read-only self-check | data initialization unless check-only | portable clone, no-mutation |
| `install_global.py` | optional one-time Codex registration | Codex home only on apply | temp-home install |

Quality gates: no runtime dependency outside Python standard library; hot index ≤2K; candidates ≤3; short candidate ≤300; 25K-event log 下 Stop manual-mode p95 below configured budget; same task ID survives at least five compactions; concurrent workers process each job once; crashed lease recovers; copied folder works and self-tests from a path containing spaces; check-only does not write; installer preserves unrelated host files and existing data.
