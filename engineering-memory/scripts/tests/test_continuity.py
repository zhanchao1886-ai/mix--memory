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

from _memory_common import estimate_tokens  # noqa: E402


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


class ContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="engineering-memory-continuity-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.data = self.temp / "data"
        self.project = "continuity-demo"
        self.task = "same-task-42"

    def continuity(self, command: str, *args: str) -> dict:
        return cli(
            "context_continuity.py",
            command,
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            self.task,
            *args,
        )

    def hook(self, payload: dict, host: str = "codex") -> dict:
        return cli(
            "hook_router.py",
            "handle",
            "--root",
            self.data,
            "--host",
            host,
            stdin=payload,
        )

    def test_explicit_capsule_is_bounded_redacted_and_line_structured(self) -> None:
        self.continuity(
            "checkpoint",
            "--goal",
            "Continue portable memory work",
            "--decision",
            "Keep Stop foreground-only",
            "--open-loop",
            "Verify api_key=verysecretvalue123",
            "--artifact",
            "/Users/example/private/project/report.md",
            "--next-action",
            "Run the queue tests",
        )
        resumed = self.continuity("resume")
        self.assertLessEqual(resumed["estimated_tokens"], resumed["limit_tokens"])
        self.assertIn("\nGoal:", resumed["context"])
        self.assertIn("[REDACTED]", resumed["context"])
        self.assertIn("report.md", resumed["context"])
        serialized = (self.data / "projects" / self.project / "continuity" / f"{self.task}.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("verysecretvalue123", serialized)

    def test_pretty_json_and_jsonl_transcripts_are_captured(self) -> None:
        pretty = self.temp / "transcript.json"
        pretty.write_text(
            json.dumps(
                {
                    "messages": [
                        {"role": "user", "content": "Preserve the current task identity"},
                        {"role": "assistant", "content": "I will store a compact capsule"},
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.continuity("capture", "--trigger", "PreCompact", "--transcript-path", str(pretty))
        shown = self.continuity("show")
        turns = [item["text"] for item in shown["recent_turns"]]
        self.assertIn("Preserve the current task identity", turns)
        self.assertIn("I will store a compact capsule", turns)

        jsonl = self.temp / "transcript.jsonl"
        jsonl.write_text(
            json.dumps({"role": "user", "text": "Resume without a new task"}) + "\n",
            encoding="utf-8",
        )
        self.continuity("capture", "--trigger", "PreCompact", "--transcript-path", str(jsonl))
        shown = self.continuity("show")
        self.assertIn("Resume without a new task", [item["text"] for item in shown["recent_turns"]])

    def test_same_task_survives_five_compactions(self) -> None:
        self.hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "hook_event_id": "goal-event",
                "session_id": self.task,
                "project": self.project,
                "prompt": "Build a stable public memory skill",
            }
        )
        for number in range(5):
            self.hook(
                {
                    "hook_event_name": "PreCompact",
                    "hook_event_id": f"pre-{number}",
                    "session_id": self.task,
                    "project": self.project,
                    "trigger": "auto",
                }
            )
            resumed = self.hook(
                {
                    "hook_event_name": "SessionStart",
                    "hook_event_id": f"resume-{number}",
                    "session_id": self.task,
                    "project": self.project,
                    "source": "compact",
                }
            )
            context = resumed["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Same task: same-task-42", context)
            self.assertIn("Build a stable public memory skill", context)
            self.assertLessEqual(estimate_tokens(context), 1200)
        shown = self.continuity("show")
        self.assertEqual(shown["compression_count"], 5)

    def test_task_ids_are_canonical_and_isolated(self) -> None:
        first = cli(
            "context_continuity.py",
            "checkpoint",
            "--root",
            self.data,
            "--project",
            "project / alpha",
            "--task-id",
            "task / one",
            "--goal",
            "First task",
        )
        second = cli(
            "context_continuity.py",
            "checkpoint",
            "--root",
            self.data,
            "--project",
            "project / alpha",
            "--task-id",
            "task / two",
            "--goal",
            "Second task",
        )
        self.assertNotEqual(first["path"], second["path"])
        self.assertNotIn("..", first["path"])
        self.assertEqual(cli(
            "context_continuity.py",
            "resume",
            "--root",
            self.data,
            "--project",
            "project / alpha",
            "--task-id",
            "task / one",
        )["task_id"], "task-one")


if __name__ == "__main__":
    unittest.main()
