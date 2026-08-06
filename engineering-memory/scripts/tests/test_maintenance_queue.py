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
)
from maintenance_queue import claim_next_job, enqueue_consolidation, queue_counts  # noqa: E402


def cli(script: str, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *map(str, args)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"{script} failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


class MaintenanceQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="engineering-memory-queue-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.data = self.temp / "data"
        ensure_layout(self.data)

    def make_due(self, project: str, task: str) -> None:
        append_event(
            self.data,
            {
                "event": "queue_test_activity",
                "event_id": event_id("queue_test_activity", project, task),
                "project": project,
                "task_id": task,
            },
            activity_tokens=131072,
        )

    def test_enqueue_is_idempotent(self) -> None:
        self.make_due("idem", "task-1")
        first = enqueue_consolidation(self.data, "idem", task_id="task-1")
        second = enqueue_consolidation(self.data, "idem", task_id="task-1")
        self.assertTrue(first["queued"])
        self.assertFalse(second["queued"])
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(queue_counts(self.data)["pending"], 1)

    def test_expired_running_lease_is_recovered_after_worker_crash(self) -> None:
        config = load_config(self.data)
        config["background"]["lease_seconds"] = 0
        atomic_write_json(self.data / "config.json", config, self.data)
        self.make_due("recover", "crashed-task")
        enqueue_consolidation(self.data, "recover", task_id="crashed-task")
        claimed = claim_next_job(self.data)
        self.assertIsNotNone(claimed)
        self.assertEqual(queue_counts(self.data)["running"], 1)
        result = cli("maintenance_worker.py", "--root", self.data, "--once")
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["jobs"][0]["state"], "done")
        self.assertEqual(result["queue"]["running"], 0)

    def test_failed_job_redacts_secret_and_absolute_path(self) -> None:
        config = load_config(self.data)
        config["background"]["max_attempts"] = 1
        atomic_write_json(self.data / "config.json", config, self.data)
        self.make_due("failure", "bad-task")
        queued = enqueue_consolidation(self.data, "failure", task_id="bad-task")
        pending = self.data / "jobs" / "pending" / f"{queued['job_id']}.json"
        payload = json.loads(pending.read_text(encoding="utf-8"))
        payload["kind"] = "/Users/private/tool api_key=verysecretvalue123"
        atomic_write_json(pending, payload, self.data)
        result = cli("maintenance_worker.py", "--root", self.data, "--once")
        self.assertEqual(result["jobs"][0]["state"], "failed")
        failed = json.loads((self.data / "jobs" / "failed" / pending.name).read_text(encoding="utf-8"))
        self.assertIn("[REDACTED]", failed["error"])
        self.assertNotIn("/Users/", failed["error"])
        self.assertNotIn("verysecretvalue123", failed["error"])

    def test_four_concurrent_workers_process_two_jobs_once(self) -> None:
        for number in range(2):
            project = f"parallel-{number}"
            task = f"task-{number}"
            self.make_due(project, task)
            enqueue_consolidation(self.data, project, task_id=task)
        commands = [
            [sys.executable, str(SCRIPTS / "maintenance_worker.py"), "--root", str(self.data), "--once"]
            for _ in range(4)
        ]
        processes = [subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for command in commands]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sum(item["processed"] for item in results), 2)
        self.assertEqual(queue_counts(self.data), {"pending": 0, "running": 0, "done": 2, "failed": 0})


if __name__ == "__main__":
    unittest.main()
