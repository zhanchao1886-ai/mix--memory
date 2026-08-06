from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _memory_common import atomic_write_json, ensure_layout, load_config  # noqa: E402


def hook(root: Path, payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "hook_router.py"), "handle", "--root", str(root)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"hook failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


class StopPerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="engineering-memory-stop-performance-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.data = self.temp / "data"
        ensure_layout(self.data)
        config = load_config(self.data)
        config["background"]["mode"] = "manual"
        config["background"]["auto_spawn"] = False
        atomic_write_json(self.data / "config.json", config, self.data)

    def test_stop_p95_stays_below_budget_with_large_usage_log(self) -> None:
        seed = [
            json.dumps(
                {
                    "event": "historical_event",
                    "event_id": f"EV-SEED-{number:08d}",
                    "time": "2026-01-01T00:00:00+08:00",
                    "activity_tokens": 0,
                },
                sort_keys=True,
            )
            for number in range(25000)
        ]
        (self.data / "usage-log.jsonl").write_text("\n".join(seed) + "\n", encoding="utf-8")
        elapsed = []
        # Twenty samples make nearest-rank p95 meaningful: one cold cache build is
        # measured but does not incorrectly become both p95 and max.
        for number in range(20):
            result = hook(
                self.data,
                {
                    "hook_event_name": "Stop",
                    "hook_event_id": f"performance-stop-{number}",
                    "session_id": "performance-task",
                    "project": "performance-project",
                    "last_assistant_message": "记忆备份：未产生；索引：无需更新。",
                },
            )
            elapsed.append(result["engineering_memory"]["maintenance"]["stop_elapsed_ms"])
        ordered = sorted(elapsed)
        p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
        budget = load_config(self.data)["background"]["stop_budget_ms"]
        if os.environ.get("ENGINEERING_MEMORY_SHOW_BENCHMARK") == "1":
            print(f"stop_p95_ms={p95:.3f} cold_or_max_ms={max(elapsed):.3f} samples={elapsed}")
        self.assertLess(p95, budget, f"p95={p95}ms samples={elapsed}")
        self.assertLess(max(elapsed), budget * 2, f"cold/max={max(elapsed)}ms samples={elapsed}")


if __name__ == "__main__":
    unittest.main()
