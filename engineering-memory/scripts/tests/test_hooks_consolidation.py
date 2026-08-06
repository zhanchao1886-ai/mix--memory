from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _memory_common import (  # noqa: E402
    append_event,
    atomic_write_json,
    ensure_layout,
    event_id,
    load_config,
    load_state,
)


def cli(script: str, *args: str, stdin: dict | None = None) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *map(str, args)],
        input=json.dumps(stdin, ensure_ascii=False) if stdin is not None else None,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"{script} failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


class HookAndConsolidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="engineering-memory-hooks-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.data = self.temp / "data"
        self.project = "hook-demo"
        ensure_layout(self.data)
        config = load_config(self.data)
        config["background"]["mode"] = "manual"
        config["background"]["auto_spawn"] = False
        atomic_write_json(self.data / "config.json", config, self.data)

    def hook(self, payload: dict) -> dict:
        return cli("hook_router.py", "handle", "--root", self.data, stdin=payload)

    def candidate(self, task_id: str = "candidate-task") -> dict:
        return cli(
            "record_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            task_id,
            "--mode",
            "candidate",
            "--title",
            "Deferred decision",
            "--category",
            "decision",
            "--content",
            "Use a watermark and preserve the remainder.",
        )

    def test_user_prompt_is_idempotent_and_injects_context(self) -> None:
        event = {
            "hook_event_name": "UserPromptSubmit",
            "hook_event_id": "fixed-event",
            "session_id": "session-1",
            "project": self.project,
            "prompt": "Please fix the index",
        }
        first = self.hook(event)
        state_once = load_state(self.data, self.project)
        second = self.hook(event)
        state_twice = load_state(self.data, self.project)
        self.assertIn("additionalContext", first["hookSpecificOutput"])
        self.assertEqual(first, second)
        self.assertEqual(state_once["unconsolidated_tokens"], state_twice["unconsolidated_tokens"])

    def test_stop_blocks_once_then_stop_hook_active_allows(self) -> None:
        base = {
            "hook_event_name": "Stop",
            "session_id": "stop-session",
            "project": self.project,
            "last_assistant_message": "Task complete without a receipt.",
        }
        blocked = self.hook(base)
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("engineering_memory_recorder", blocked["reason"])
        base["stop_hook_active"] = True
        allowed = self.hook(base)
        self.assertEqual(allowed["engineering_memory"]["status"], "allowed")

    def test_receipt_or_finalize_allows_stop(self) -> None:
        receipt_allowed = self.hook(
            {
                "hook_event_name": "Stop",
                "session_id": "receipt-session",
                "project": self.project,
                "last_assistant_message": "记忆备份：未产生；索引：无需更新。",
            }
        )
        self.assertEqual(receipt_allowed["engineering_memory"]["status"], "allowed")
        cli(
            "hook_router.py",
            "finalize",
            "--root",
            self.data,
            "--task-id",
            "finalized-session",
            "--project",
            self.project,
        )
        finalized_allowed = self.hook(
            {
                "hook_event_name": "Stop",
                "session_id": "finalized-session",
                "project": self.project,
                "last_assistant_message": "done",
            }
        )
        self.assertEqual(finalized_allowed["engineering_memory"]["status"], "allowed")

    def test_pre_and_post_compact_preserve_watermark_context(self) -> None:
        pre = self.hook(
            {
                "hook_event_name": "PreCompact",
                "hook_event_id": "pre-1",
                "session_id": "compact-session",
                "project": self.project,
            }
        )
        self.assertTrue(pre["continue"])
        self.assertIn("continuity", pre["engineering_memory"])
        post = self.hook(
            {
                "hook_event_name": "PostCompact",
                "hook_event_id": "post-1",
                "session_id": "compact-session",
                "project": self.project,
            }
        )
        self.assertTrue(post["continue"])
        self.assertIn("SessionStart", post["systemMessage"])
        resumed = self.hook(
            {
                "hook_event_name": "SessionStart",
                "hook_event_id": "start-compact-1",
                "session_id": "compact-session",
                "project": self.project,
                "source": "compact",
            }
        )
        context = resumed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Same task: compact-session", context)
        self.assertIn("Continue this task in place", context)

    def test_exact_128k_threshold(self) -> None:
        append_event(
            self.data,
            {
                "event": "synthetic_activity",
                "event_id": event_id("synthetic", "131071"),
                "project": self.project,
                "task_id": "threshold",
            },
            activity_tokens=131071,
        )
        self.assertEqual(load_state(self.data, self.project)["maintenance_due"], "soon")
        append_event(
            self.data,
            {
                "event": "synthetic_activity",
                "event_id": event_id("synthetic", "plus-one"),
                "project": self.project,
                "task_id": "threshold",
            },
            activity_tokens=1,
        )
        state = load_state(self.data, self.project)
        self.assertEqual(state["unconsolidated_tokens"], 131072)
        self.assertEqual(state["maintenance_due"], "due")

    def test_consolidation_promotes_deferred_and_monitors_remainder(self) -> None:
        candidate = self.candidate()
        before = load_state(self.data, self.project)["unconsolidated_tokens"]
        append_event(
            self.data,
            {
                "event": "large_activity",
                "event_id": event_id("large_activity", "first"),
                "project": self.project,
                "task_id": "large-task",
            },
            activity_tokens=140000,
        )
        first = cli("consolidate_project.py", "--root", self.data, "--project", self.project)
        self.assertEqual(first["candidate_ids"], [candidate["candidate_id"]])
        self.assertEqual(len(first["promoted"]), 1)
        state = load_state(self.data, self.project)
        self.assertEqual(state["unconsolidated_tokens"], before + 140000 - 131072)
        self.assertGreater(state["checkpoint_token_offset"], 0)
        self.assertEqual(state["maintenance_due"], "no")
        # The remainder remains live; adding exactly enough activity reaches 128K again.
        needed = 131072 - state["unconsolidated_tokens"]
        append_event(
            self.data,
            {
                "event": "large_activity",
                "event_id": event_id("large_activity", "second"),
                "project": self.project,
                "task_id": "large-task-2",
            },
            activity_tokens=needed,
        )
        self.assertEqual(load_state(self.data, self.project)["maintenance_due"], "due")
        second = cli("consolidate_project.py", "--root", self.data, "--project", self.project)
        self.assertEqual(second["remaining_tokens"], 0)
        self.assertEqual(load_state(self.data, self.project)["maintenance_due"], "no")

    def test_rejected_candidate_never_promotes(self) -> None:
        candidate = self.candidate("reject-task")
        cli(
            "record_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "reject-task",
            "--mode",
            "rejected",
            "--candidate-id",
            candidate["candidate_id"],
        )
        append_event(
            self.data,
            {
                "event": "large_activity",
                "event_id": event_id("large_activity", "reject"),
                "project": self.project,
                "task_id": "large-reject",
            },
            activity_tokens=131072,
        )
        result = cli("consolidate_project.py", "--root", self.data, "--project", self.project)
        self.assertEqual(result["promoted"], [])
        memory_dir = self.data / "projects" / self.project / "memories"
        self.assertFalse(memory_dir.exists() and list(memory_dir.glob("*.md")))

    def test_stop_hook_only_queues_then_worker_consolidates(self) -> None:
        self.candidate("auto-task")
        append_event(
            self.data,
            {
                "event": "large_activity",
                "event_id": event_id("large_activity", "auto"),
                "project": self.project,
                "task_id": "auto-task",
            },
            activity_tokens=131072,
        )
        result = self.hook(
            {
                "hook_event_name": "Stop",
                "session_id": "auto-task",
                "project": self.project,
                "last_assistant_message": "记忆备份：候选 1 条；索引：无需更新。",
            }
        )
        maintenance = result["engineering_memory"]["maintenance"]
        self.assertTrue(maintenance["queued"])
        self.assertEqual(maintenance["job_state"], "pending")
        self.assertFalse(maintenance["worker"]["spawned"])
        memory_dir = self.data / "projects" / self.project / "memories"
        self.assertFalse(memory_dir.exists() and list(memory_dir.glob("*.md")))
        drained = cli("maintenance_worker.py", "--root", self.data, "--once")
        self.assertEqual(drained["processed"], 1)
        self.assertEqual(drained["jobs"][0]["state"], "done")
        self.assertEqual(len(list(memory_dir.glob("*.md"))), 1)
        self.assertLess(load_state(self.data, self.project)["unconsolidated_tokens"], 131072)

    def test_worker_never_crosses_stop_watermark(self) -> None:
        config = load_config(self.data)
        config["consolidation"]["chunk_tokens"] = 65536
        config["consolidation"]["max_chunks_per_run"] = 3
        atomic_write_json(self.data / "config.json", config, self.data)
        append_event(
            self.data,
            {
                "event": "large_activity",
                "event_id": event_id("large_activity", "watermark-before"),
                "project": self.project,
                "task_id": "watermark-task",
            },
            activity_tokens=131072,
        )
        stop = self.hook(
            {
                "hook_event_name": "Stop",
                "hook_event_id": "watermark-stop",
                "session_id": "watermark-task",
                "project": self.project,
                "last_assistant_message": "记忆备份：未产生；索引：无需更新。",
            }
        )
        watermark = stop["engineering_memory"]["maintenance"]["watermark_seq"]
        candidate = self.candidate("after-watermark")
        self.assertGreater(
            max(event.get("seq", 0) for event in json.loads("[" + ",".join(
                line for line in (self.data / "usage-log.jsonl").read_text(encoding="utf-8").splitlines()
            ) + "]") if event.get("candidate_id") == candidate["candidate_id"]),
            watermark,
        )
        drained = cli("maintenance_worker.py", "--root", self.data, "--once")
        self.assertEqual(drained["jobs"][0]["state"], "done")
        memory_dir = self.data / "projects" / self.project / "memories"
        self.assertFalse(memory_dir.exists() and list(memory_dir.glob("*.md")))

    def test_recorder_subagent_must_emit_backup_receipt(self) -> None:
        base = {
            "hook_event_name": "SubagentStop",
            "session_id": "recorder-session",
            "project": self.project,
            "agent_id": "agent-1",
            "agent_type": "engineering_memory_recorder",
            "last_assistant_message": "recorded one candidate",
        }
        blocked = self.hook(base)
        self.assertEqual(blocked["decision"], "block")
        base["last_assistant_message"] = "记忆备份：候选 1 条；索引：无需更新。"
        allowed = self.hook(base)
        self.assertEqual(allowed, {})


if __name__ == "__main__":
    unittest.main()
