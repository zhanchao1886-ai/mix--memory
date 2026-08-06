#!/usr/bin/env python3
"""Initialize and self-check a downloaded Engineering Memory folder in place."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from _memory_common import MemoryErrorBase, ensure_layout, load_config, print_json, resolve_data_root


REQUIRED = (
    "SKILL.md",
    "agents/openai.yaml",
    "data/config.json",
    "scripts/_memory_common.py",
    "scripts/record_memory.py",
    "scripts/index_memory.py",
    "scripts/search_memory.py",
    "scripts/mark_memory.py",
    "scripts/context_continuity.py",
    "scripts/maintenance_queue.py",
    "scripts/maintenance_worker.py",
    "scripts/hook_router.py",
    "scripts/adapters/codex.py",
    "scripts/adapters/workbuddy.py",
)


def tree_fingerprint(skill_root: Path) -> str:
    digest = hashlib.sha256()
    for relative in REQUIRED:
        path = skill_root / relative
        digest.update(relative.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def check_tree(skill_root: Path, data_root: Path, *, read_only: bool = False) -> dict[str, Any]:
    missing = [relative for relative in REQUIRED if not (skill_root / relative).is_file()]
    errors = []
    config: dict[str, Any] = {}
    config_path = data_root / "config.json"
    if read_only:
        if not config_path.is_file():
            errors.append("data/config.json is missing")
        else:
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                errors.append(f"invalid data/config.json: {exc}")
            else:
                if isinstance(loaded, dict):
                    config = loaded
                else:
                    errors.append("data/config.json must contain an object")
    else:
        config = load_config(data_root)
    thresholds = config.get("consolidation", {})
    if int(thresholds.get("hard_tokens", 0)) != 131072:
        errors.append("consolidation.hard_tokens must be 131072")
    if float(config.get("limits", {}).get("pipeline_hard_ratio", 0)) != 0.30:
        errors.append("limits.pipeline_hard_ratio must be 0.30")
    if missing:
        errors.append("missing required files")
    return {
        "ready": not errors,
        "missing": missing,
        "errors": errors,
        "schema_version": config.get("schema_version"),
        "fingerprint": tree_fingerprint(skill_root),
        "portable_data_root": "data/",
        "dependencies": "python-standard-library-only",
    }


def integration(host: str) -> dict[str, Any]:
    if host == "codex":
        return {
            "standalone_ready": True,
            "full_lifecycle_registration": [
                "python3 scripts/install_global.py --dry-run",
                "python3 scripts/install_global.py --apply",
            ],
            "note": "Copying into a discovered skills directory enables the workflow; lifecycle hooks need one-time registration and trust review.",
        }
    if host in {"workbuddy", "generic"}:
        return {
            "standalone_ready": True,
            "event_bridge": f"python3 scripts/hook_router.py handle --host {host}",
            "protocol": "engineering-memory.host-event.v1",
            "note": "Map host lifecycle events to the public JSON contract in references/host-adapters.md.",
        }
    return {
        "standalone_ready": True,
        "commands": {
            "record": "python3 scripts/record_memory.py ...",
            "search": "python3 scripts/search_memory.py ...",
            "resume": "python3 scripts/context_continuity.py resume ...",
            "worker": "python3 scripts/maintenance_worker.py --drain",
        },
    }


def run_self_tests(skill_root: Path) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["ENGINEERING_MEMORY_BOOTSTRAP_SELF_TEST"] = "1"
    result = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "scripts/tests", "-q"],
        cwd=skill_root,
        text=True,
        capture_output=True,
        timeout=120,
        env=environment,
    )
    return {
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="data directory; defaults to this downloaded folder's data/")
    parser.add_argument("--host", choices=("standalone", "codex", "workbuddy", "generic"), default="standalone")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    skill_root = Path(__file__).resolve().parent.parent
    requested_root = Path(args.root).expanduser() if args.root else skill_root / "data"
    data_root = requested_root.resolve()
    try:
        if not args.check_only:
            data_root = resolve_data_root(data_root)
            ensure_layout(data_root)
        result = check_tree(skill_root, data_root, read_only=args.check_only)
        result["host"] = args.host
        result["integration"] = integration(args.host)
        if args.self_test:
            result["self_test"] = run_self_tests(skill_root)
            result["ready"] = result["ready"] and result["self_test"]["passed"]
        print_json(result)
        return 0 if result["ready"] else 1
    except (MemoryErrorBase, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
