#!/usr/bin/env python3
"""Idempotently register Engineering Memory in a Codex home.

This is the only script in the Skill allowed to write outside data/.  It is
dry-run by default.  Use --apply only after explicit user approval.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


START = "<!-- engineering-memory:start -->"
END = "<!-- engineering-memory:end -->"
HOOK_MARKER = "engineering-memory/scripts/hook_router.py"
AGENT_FILES = (
    "engineering-memory-recorder.toml",
    "engineering-memory-indexer.toml",
    "engineering-memory-cat.toml",
)


class InstallError(RuntimeError):
    pass


def source_root(value: str | None) -> Path:
    root = Path(value).expanduser().resolve() if value else Path(__file__).resolve().parent.parent
    if not (root / "SKILL.md").is_file() or not (root / "data" / "config.json").is_file():
        raise InstallError(f"not an engineering-memory source root: {root}")
    return root


def codex_home(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallError(f"invalid JSON, refusing to overwrite {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstallError(f"expected JSON object in {path}")
    return payload


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.engineering-memory.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def merge_agents_text(current: str, block: str, *, remove: bool = False) -> str:
    start = current.find(START)
    end = current.find(END, start + len(START)) if start >= 0 else -1
    if (start >= 0) != (end >= 0):
        raise InstallError("AGENTS.md contains an incomplete engineering-memory managed block")
    if start >= 0 and end >= 0:
        end += len(END)
        before = current[:start].rstrip()
        after = current[end:].lstrip("\n")
        current = (before + ("\n\n" if before and after else "") + after).rstrip() + ("\n" if before or after else "")
    if remove:
        return current
    base = current.rstrip()
    return (base + "\n\n" if base else "") + block.strip() + "\n"


def is_managed_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks", [])
    return any(
        isinstance(hook, dict) and HOOK_MARKER in str(hook.get("command", ""))
        for hook in hooks
    )


def merge_hooks(
    current: dict[str, Any], template: dict[str, Any], *, remove: bool = False
) -> dict[str, Any]:
    merged = json.loads(json.dumps(current))
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError("hooks.json field 'hooks' must be an object")
    template_hooks = template.get("hooks", {})
    all_events = set(hooks) | set(template_hooks)
    for event in sorted(all_events):
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            raise InstallError(f"hooks.json event {event} must be a list")
        kept = [group for group in existing if not is_managed_group(group)]
        if not remove:
            kept.extend(template_hooks.get(event, []))
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        merged.pop("hooks", None)
    return merged


def source_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if relative == Path("data/.engineering-memory.lock"):
            continue
        if relative.parts[:2] == ("data", "runtime"):
            continue
        if relative.parts[:3] == ("data", "jobs", "running"):
            continue
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            continue
        files.append(path)
    return sorted(files)


def copy_skill(root: Path, target: Path) -> dict[str, int]:
    copied = 0
    preserved_data = 0
    for path in source_files(root):
        relative = path.relative_to(root)
        destination = target / relative
        if relative.parts and relative.parts[0] == "data" and destination.exists():
            preserved_data += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied += 1
    return {"copied_files": copied, "preserved_data_files": preserved_data}


def plan(root: Path, home: Path, remove: bool) -> dict[str, Any]:
    target = home / "skills" / "engineering-memory"
    return {
        "mode": "unregister" if remove else "install_or_upgrade",
        "source": str(root),
        "codex_home": str(home),
        "skill_target": str(target),
        "preserve_existing_data": True,
        "agents": [str(home / "agents" / name) for name in AGENT_FILES],
        "agents_file": str(home / "AGENTS.md"),
        "hooks_file": str(home / "hooks.json"),
        "writes_outside_data": True,
    }


def apply_install(root: Path, home: Path) -> dict[str, Any]:
    target = home / "skills" / "engineering-memory"
    copy_result = copy_skill(root, target)
    agents_dir = home / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for name in AGENT_FILES:
        shutil.copy2(root / "templates" / "agents" / name, agents_dir / name)
    block = read_text(root / "templates" / "AGENTS.block.md")
    agents_path = home / "AGENTS.md"
    merged_agents = merge_agents_text(read_text(agents_path), block)
    write_text(agents_path, merged_agents)
    hooks_path = home / "hooks.json"
    merged_hooks = merge_hooks(read_json(hooks_path), read_json(root / "hooks" / "hooks.template.json"))
    write_json(hooks_path, merged_hooks)
    return {
        **plan(root, home, False),
        **copy_result,
        "status": "applied",
    }


def apply_remove(root: Path, home: Path) -> dict[str, Any]:
    agents_path = home / "AGENTS.md"
    if agents_path.exists():
        write_text(agents_path, merge_agents_text(read_text(agents_path), "", remove=True))
    hooks_path = home / "hooks.json"
    if hooks_path.exists():
        current = read_json(hooks_path)
        cleaned = merge_hooks(current, {}, remove=True)
        write_json(hooks_path, cleaned)
    removed_agents = []
    for name in AGENT_FILES:
        path = home / "agents" / name
        if path.exists():
            path.unlink()
            removed_agents.append(str(path))
    return {
        **plan(root, home, True),
        "status": "unregistered",
        "removed_agents": removed_agents,
        "skill_and_data_preserved": (home / "skills" / "engineering-memory").exists(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="perform registration")
    action.add_argument("--remove", action="store_true", help="unregister, preserving Skill and data")
    action.add_argument("--dry-run", action="store_true", help="print the plan without writing")
    parser.add_argument("--source-root")
    parser.add_argument("--codex-home")
    args = parser.parse_args(argv)
    try:
        root = source_root(args.source_root)
        home = codex_home(args.codex_home)
        if args.apply:
            result = apply_install(root, home)
        elif args.remove:
            result = apply_remove(root, home)
        else:
            result = {**plan(root, home, False), "status": "dry_run", "mutated": False}
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (InstallError, OSError, shutil.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
