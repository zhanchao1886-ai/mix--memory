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

from _memory_common import estimate_tokens, parse_frontmatter  # noqa: E402


def cli(script: str, *args: str, check: bool = True) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *map(str, args)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{script} failed ({result.returncode}): {result.stderr}\n{result.stdout}")
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result, payload


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class CorePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="engineering-memory-core-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.data = self.temp / "data"
        self.project = "demo"

    def record(self, title: str, content: str, category: str = "decision", tags: str = "alpha") -> dict:
        _, payload = cli(
            "record_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            f"record-{title}",
            "--mode",
            "locked",
            "--title",
            title,
            "--category",
            category,
            "--tags",
            tags,
            "--content",
            content,
        )
        return payload

    def cat_for(self, memory_id: str) -> str:
        path = self.data / "projects" / self.project / "memories" / f"{memory_id}.md"
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        return str(meta["cat"])

    def test_record_index_tag_fidelity_and_safe_serialization(self) -> None:
        payload = self.record(
            "Portable index",
            "api_key=supersecretvalue lives near /Users/example/private/repo/index.py",
            tags="Portable,Index",
        )
        memory_path = self.data / payload["path"]
        text = memory_path.read_text(encoding="utf-8")
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("supersecretvalue", text)
        self.assertNotIn("/Users/", text)
        self.assertNotIn("CAT:", (self.data / "usage-log.jsonl").read_text(encoding="utf-8"))
        index = json.loads((self.data / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["entries"][0]["tags"], ["portable", "index"])
        self.assertFalse(Path(index["entries"][0]["path"]).is_absolute())
        checked, report = cli("index_memory.py", "--root", self.data, "--check")
        self.assertEqual(checked.returncode, 0)
        self.assertTrue(report["valid"])

    def test_short_candidate_limit_and_lock_choice(self) -> None:
        _, candidate = cli(
            "record_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "short-1",
            "--mode",
            "candidate",
            "--title",
            "Short lesson",
            "--category",
            "lesson",
            "--content",
            "Use the incremental index after one write.",
        )
        self.assertEqual(candidate["status"], "candidate")
        self.assertIn("锁定", candidate["lock_prompt"])
        self.assertEqual(list((self.data / "projects" / self.project).glob("memories/*.md")), [])
        failed, _ = cli(
            "record_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--mode",
            "candidate",
            "--title",
            "Too long",
            "--category",
            "lesson",
            "--content",
            "中" * 301,
            check=False,
        )
        self.assertEqual(failed.returncode, 2)
        self.assertIn("limit is 300", failed.stderr)

    def test_hot_index_cap_and_search_limit(self) -> None:
        # Initialize and then tighten the cap while retaining enough room for one row.
        self.record("seed", "seed memory")
        config_path = self.data / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["limits"]["hot_index_tokens"] = 250
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for number in range(8):
            self.record(f"shared topic {number}", f"Known answer number {number}", tags="shared,topic")
        _, rebuilt = cli("index_memory.py", "--root", self.data)
        self.assertLessEqual(rebuilt["hot_tokens"], 250)
        self.assertLess(rebuilt["hot_entries"], rebuilt["entries"])
        _, found = cli("search_memory.py", "shared", "--root", self.data, "--limit", "99")
        self.assertLessEqual(found["count"], 3)
        self.assertEqual(found["limit"], 3)
        self.assertEqual(found["layer"], "cold")

    def test_cat_state_machine_modification_conflict_and_dry_run(self) -> None:
        memory = self.record("CAT evidence", "Original durable fact.")
        memory_id = memory["memory_id"]
        cli(
            "search_memory.py",
            "CAT evidence",
            "--root",
            self.data,
            "--project",
            self.project,
            "--log-usage",
            "--task-id",
            "use-1",
        )
        _, first = cli(
            "mark_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "use-1",
            "--used",
            memory_id,
        )
        self.assertEqual(first["metrics"]["candidate_utilization"], 1.0)
        self.assertIn(":observed:1:", self.cat_for(memory_id))
        _, duplicate = cli(
            "mark_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "use-1",
            "--used",
            memory_id,
        )
        self.assertEqual(duplicate["changes"][0]["transition"], "duplicate_task_ignored")
        self.assertIn(":observed:1:", self.cat_for(memory_id))
        cli(
            "mark_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "use-2",
            "--used",
            memory_id,
        )
        self.assertIn(":stable:2:", self.cat_for(memory_id))

        memory_path = self.data / memory["path"]
        memory_path.write_text(
            memory_path.read_text(encoding="utf-8").replace("Original durable fact.", "Modified durable fact."),
            encoding="utf-8",
        )
        before = snapshot(self.data)
        _, preview = cli(
            "mark_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "scan-preview",
            "--scan",
            "--dry-run",
        )
        self.assertTrue(preview["changes"])
        self.assertEqual(before, snapshot(self.data))
        cli(
            "mark_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "scan-real",
            "--scan",
        )
        self.assertIn(":unobserved:0:", self.cat_for(memory_id))
        self.assertNotIn("CAT:", (self.data / "usage-log.jsonl").read_text(encoding="utf-8"))
        cli(
            "mark_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "after-modification",
            "--used",
            memory_id,
        )
        self.assertIn(":observed:1:", self.cat_for(memory_id))
        cli(
            "mark_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "conflict-task",
            "--conflict",
            memory_id,
            "--reason",
            "new evidence disagrees",
        )
        self.assertIn(":unobserved:0:", self.cat_for(memory_id))

    def test_budget_boundary(self) -> None:
        _, exact = cli(
            "mark_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "budget-30",
            "--pipeline-tokens",
            "300",
            "--task-total-tokens",
            "1000",
        )
        self.assertEqual(exact["budget"]["ratio"], 0.3)
        self.assertNotEqual(exact["budget"]["status"], "hard_exceeded")
        self.assertEqual(exact["budget"]["policy"]["mode"], "guarded")
        self.assertIn("full_rebuild", exact["budget"]["policy"]["suppress"])
        _, over = cli(
            "mark_memory.py",
            "--root",
            self.data,
            "--project",
            self.project,
            "--task-id",
            "budget-over",
            "--pipeline-tokens",
            "301",
            "--task-total-tokens",
            "1000",
        )
        self.assertEqual(over["budget"]["status"], "hard_exceeded")
        self.assertTrue(over["budget"]["degraded"])
        self.assertEqual(over["budget"]["policy"]["mode"], "minimal")
        self.assertIn("candidate_write", over["budget"]["policy"]["suppress"])

    def test_golden_hit_at_3_meets_target(self) -> None:
        cases = {
            "crimsonfalcon": "Red deployment switch",
            "azurewhale": "Blue migration rule",
            "greentiger": "Green retry policy",
            "silverowl": "Silver index decision",
            "goldenfox": "Golden CAT lesson",
        }
        ids = {}
        for keyword, title in cases.items():
            ids[keyword] = self.record(title, f"The unique key is {keyword}.", tags=keyword)["memory_id"]
        hits = 0
        for keyword, expected_id in ids.items():
            _, result = cli("search_memory.py", keyword, "--root", self.data, "--limit", "3")
            hits += expected_id in [item["id"] for item in result["results"]]
        hit_at_3 = hits / len(ids)
        self.assertGreaterEqual(hit_at_3, 0.8)

    def test_orphan_path_check_fails_safely(self) -> None:
        self.record("orphan test", "A valid memory first.")
        path = self.data / "index.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["entries"][0]["path"] = "projects/demo/memories/missing.md"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result, report = cli("index_memory.py", "--root", self.data, "--check", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report["valid"])
        self.assertTrue(report["missing_paths"])

    def test_token_estimator_is_conservative_for_cjk_and_ascii(self) -> None:
        self.assertEqual(estimate_tokens("中" * 300), 300)
        self.assertEqual(estimate_tokens("x" * 400), 100)


if __name__ == "__main__":
    unittest.main()
