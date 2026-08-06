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
INSTALLER = SCRIPTS / "install_global.py"
MARKER = "engineering-memory/scripts/hook_router.py"


def run(command: list[str], *, cwd: Path | None = None) -> dict:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(f"command failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}


def managed_hook_count(payload: dict) -> int:
    count = 0
    for groups in payload.get("hooks", {}).values():
        for group in groups:
            count += any(MARKER in str(hook.get("command", "")) for hook in group.get("hooks", []))
    return count


class MigrationAndInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="engineering-memory-install-"))
        self.addCleanup(shutil.rmtree, self.temp, True)

    def test_whole_folder_copy_to_path_with_spaces_is_portable(self) -> None:
        migrated = self.temp / "new device with spaces" / "engineering-memory"
        shutil.copytree(SKILL_ROOT, migrated)
        record = run(
            [
                sys.executable,
                str(migrated / "scripts" / "record_memory.py"),
                "--project",
                "portable-demo",
                "--task-id",
                "migration-task",
                "--mode",
                "locked",
                "--title",
                "Portable root",
                "--category",
                "filemap",
                "--tags",
                "migration,path",
                "--content",
                "Scripts resolve data relative to their own location.",
            ],
            cwd=self.temp,
        )
        self.assertTrue((migrated / "data" / record["path"]).is_file())
        search = run(
            [
                sys.executable,
                str(migrated / "scripts" / "search_memory.py"),
                "Portable root",
                "--project",
                "portable-demo",
            ]
        )
        self.assertEqual(search["results"][0]["id"], record["memory_id"])
        serialized = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (migrated / "data").rglob("*")
            if path.is_file() and path.suffix in {".md", ".json", ".jsonl"}
        )
        self.assertNotIn(str(SKILL_ROOT), serialized)
        self.assertNotIn(str(migrated), serialized)

    def test_dry_run_does_not_create_codex_home(self) -> None:
        home = self.temp / "absent-home"
        result = run(
            [sys.executable, str(INSTALLER), "--dry-run", "--codex-home", str(home)]
        )
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["mutated"])
        self.assertFalse(home.exists())

    def test_install_upgrade_and_scoped_remove_preserve_user_state(self) -> None:
        home = self.temp / "codex-home"
        (home / "agents").mkdir(parents=True)
        (home / "agents" / "unrelated.toml").write_text("name='keep_me'\n", encoding="utf-8")
        (home / "AGENTS.md").write_text("# Existing global instruction\n", encoding="utf-8")
        existing_hooks = {
            "custom": {"keep": True},
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "echo preserve-existing-hook"}]}
                ]
            },
        }
        (home / "hooks.json").write_text(
            json.dumps(existing_hooks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        first = run([sys.executable, str(INSTALLER), "--apply", "--codex-home", str(home)])
        self.assertEqual(first["status"], "applied")
        self.assertTrue((home / "agents" / "unrelated.toml").exists())
        self.assertIn("Existing global instruction", (home / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8").count("engineering-memory:start"), 1)
        hooks = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        self.assertTrue(hooks["custom"]["keep"])
        self.assertIn("preserve-existing-hook", json.dumps(hooks))
        self.assertEqual(managed_hook_count(hooks), 8)

        sentinel = home / "skills" / "engineering-memory" / "data" / "preserve-me.txt"
        sentinel.write_text("user runtime data\n", encoding="utf-8")
        second = run([sys.executable, str(INSTALLER), "--apply", "--codex-home", str(home)])
        self.assertEqual(second["status"], "applied")
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "user runtime data\n")
        self.assertEqual((home / "AGENTS.md").read_text(encoding="utf-8").count("engineering-memory:start"), 1)
        hooks = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(managed_hook_count(hooks), 8)

        removed = run([sys.executable, str(INSTALLER), "--remove", "--codex-home", str(home)])
        self.assertEqual(removed["status"], "unregistered")
        self.assertTrue(removed["skill_and_data_preserved"])
        self.assertTrue(sentinel.exists())
        self.assertTrue((home / "agents" / "unrelated.toml").exists())
        self.assertNotIn("engineering-memory:start", (home / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("Existing global instruction", (home / "AGENTS.md").read_text(encoding="utf-8"))
        hooks = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(managed_hook_count(hooks), 0)
        self.assertIn("preserve-existing-hook", json.dumps(hooks))
        self.assertTrue(hooks["custom"]["keep"])
        for name in (
            "engineering-memory-recorder.toml",
            "engineering-memory-indexer.toml",
            "engineering-memory-cat.toml",
        ):
            self.assertFalse((home / "agents" / name).exists())


if __name__ == "__main__":
    unittest.main()
