from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = SKILL_ROOT / "scripts"


def run(command: list[str], *, stdin: dict | None = None, check: bool = True) -> tuple[int, dict]:
    result = subprocess.run(
        command,
        input=json.dumps(stdin, ensure_ascii=False) if stdin is not None else None,
        text=True,
        capture_output=True,
        timeout=150,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"command failed: {result.stderr}\n{result.stdout}")
    return result.returncode, json.loads(result.stdout) if result.stdout.strip() else {}


class AdapterAndBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="engineering-memory-adapter-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.data = self.temp / "data"

    def workbuddy(self, payload: dict, command: str = "handle") -> dict:
        return run(
            [
                sys.executable,
                str(SCRIPTS / "hook_router.py"),
                command,
                "--host",
                "workbuddy",
                "--root",
                str(self.data),
            ],
            stdin=payload,
        )[1]

    def test_workbuddy_public_contract_maps_compaction_and_same_task_resume(self) -> None:
        normalized = self.workbuddy(
            {
                "event": "before_compact",
                "event_id": "normalize-1",
                "task_id": "wb-task",
                "project": "wb-project",
            },
            "normalize",
        )
        self.assertEqual(normalized["name"], "PreCompact")
        self.assertEqual(normalized["host"], "workbuddy")
        self.workbuddy(
            {
                "event": "message_submitted",
                "event_id": "prompt-1",
                "task_id": "wb-task",
                "project": "wb-project",
                "message": "Keep this exact WorkBuddy task alive",
            }
        )
        compacted = self.workbuddy(
            {
                "event": "before_compact",
                "event_id": "compact-1",
                "task_id": "wb-task",
                "project": "wb-project",
            }
        )
        self.assertEqual(compacted["protocol"], "engineering-memory.host-event.v1")
        self.assertTrue(compacted["continue"])
        resumed = self.workbuddy(
            {
                "event": "task_resume",
                "event_id": "resume-1",
                "task_id": "wb-task",
                "project": "wb-project",
            }
        )
        self.assertIn("Same task: wb-task", resumed["additional_context"])
        self.assertIn("Keep this exact WorkBuddy task alive", resumed["additional_context"])

    def test_check_only_never_creates_explicit_missing_data_root(self) -> None:
        missing = self.temp / "must-remain-missing"
        code, payload = run(
            [
                sys.executable,
                str(SCRIPTS / "bootstrap_portable.py"),
                "--check-only",
                "--root",
                str(missing),
            ],
            check=False,
        )
        self.assertEqual(code, 1)
        self.assertFalse(payload["ready"])
        self.assertFalse(missing.exists())

    def test_copied_folder_with_spaces_bootstraps_and_self_tests(self) -> None:
        if os.environ.get("ENGINEERING_MEMORY_BOOTSTRAP_SELF_TEST") == "1":
            self.skipTest("avoid recursive bootstrap self-test")
        migrated = self.temp / "downloaded skill with spaces" / "engineering-memory"
        shutil.copytree(
            SKILL_ROOT,
            migrated,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        _, payload = run(
            [
                sys.executable,
                str(migrated / "scripts" / "bootstrap_portable.py"),
                "--host",
                "workbuddy",
                "--self-test",
            ]
        )
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["self_test"]["passed"])
        self.assertEqual(payload["integration"]["protocol"], "engineering-memory.host-event.v1")
        self.assertEqual(payload["portable_data_root"], "data/")


if __name__ == "__main__":
    unittest.main()
