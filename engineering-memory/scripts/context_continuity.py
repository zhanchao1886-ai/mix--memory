#!/usr/bin/env python3
"""Build and restore compact task-continuity capsules across context compaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from _memory_common import (
    DataLock,
    MemoryErrorBase,
    append_event,
    atomic_write_json,
    canonical_segment,
    contained_path,
    estimate_tokens,
    event_id,
    load_config,
    load_events,
    load_state,
    now_iso,
    print_json,
    read_json,
    redact_sensitive,
    resolve_data_root,
)


def clean_text(value: Any, token_limit: int) -> str:
    text = redact_sensitive(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if estimate_tokens(text) <= token_limit:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= token_limit:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def clean_context(value: Any, token_limit: int) -> str:
    """Redact and trim injected context while preserving its line structure."""
    text = redact_sensitive(str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if estimate_tokens(text) <= token_limit:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= token_limit:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def safe_artifact(value: str) -> str:
    normalized = redact_sensitive(value).strip().replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts:
        normalized = path.name
    return normalized[:300]


def capsule_path(root: Path, project: str, task_id: str) -> Path:
    return contained_path(
        root,
        Path("projects")
        / canonical_segment(project, "project")
        / "continuity"
        / f"{canonical_segment(task_id, 'task')}.json",
    )


def default_capsule(project: str, task_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project": canonical_segment(project, "project"),
        "task_id": canonical_segment(task_id, "task"),
        "revision": 0,
        "compression_count": 0,
        "goal": "",
        "decisions": [],
        "open_loops": [],
        "artifacts": [],
        "next_action": "",
        "memory_ids": [],
        "recent_turns": [],
        "watermark": {},
        "last_trigger": None,
        "updated": None,
    }


def load_capsule(root: Path, project: str, task_id: str) -> dict[str, Any]:
    path = capsule_path(root, project, task_id)
    payload = read_json(path, None)
    return payload if isinstance(payload, dict) else default_capsule(project, task_id)


def unique(items: Iterable[Any], limit: int = 20) -> list[Any]:
    result: list[Any] = []
    fingerprints: set[str] = set()
    for item in items:
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if fingerprint not in fingerprints:
            result.append(item)
            fingerprints.add(fingerprint)
    return result[-limit:]


def _write_capsule_unlocked(root: Path, capsule: dict[str, Any]) -> Path:
    capsule["revision"] = int(capsule.get("revision", 0)) + 1
    capsule["updated"] = now_iso(root)
    path = capsule_path(root, str(capsule["project"]), str(capsule["task_id"]))
    atomic_write_json(path, capsule, root)
    return path


def record_turn(
    root: Path,
    project: str,
    task_id: str,
    *,
    role: str,
    text: str,
    event_key: str | None = None,
) -> dict[str, Any]:
    project = canonical_segment(project, "project")
    task_id = canonical_segment(task_id, "task")
    config = load_config(root)
    settings = config.get("continuity", {})
    turn_limit = int(settings.get("turn_tokens", 400))
    recent_limit = int(settings.get("recent_turns", 6))
    clean = clean_text(text, turn_limit)
    if not clean:
        return load_capsule(root, project, task_id)
    event_key = event_key or event_id("continuity_turn", task_id, role, clean)
    with DataLock(root):
        capsule = load_capsule(root, project, task_id)
        turn = {"role": role, "text": clean, "event_key": event_key, "time": now_iso(root)}
        capsule["recent_turns"] = unique([*capsule.get("recent_turns", []), turn], recent_limit)
        if role == "user":
            if not capsule.get("goal"):
                capsule["goal"] = clean
            capsule["next_action"] = clean
        _write_capsule_unlocked(root, capsule)
        append_event(
            root,
            {
                "event": "continuity_turn_recorded",
                "event_id": event_id("continuity_turn_recorded", event_key),
                "project": project,
                "task_id": task_id,
                "role": role,
                "capsule_revision": capsule["revision"],
            },
            assume_locked=True,
        )
        return capsule


def _walk_messages(value: Any, inherited_role: str | None = None) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        role = str(value.get("role") or inherited_role or "").lower()
        if role not in {"user", "assistant"}:
            role = inherited_role or ""
        for key in ("text", "message"):
            item = value.get(key)
            if role in {"user", "assistant"} and isinstance(item, str) and item.strip():
                found.append((role, item))
        content = value.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            found.append((role, content))
        for child in value.values():
            if isinstance(child, (dict, list)):
                found.extend(_walk_messages(child, role or inherited_role))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_messages(child, inherited_role))
    return found


def transcript_turns(path_value: str | None, *, max_bytes: int, turn_tokens: int) -> list[dict[str, str]]:
    if not path_value:
        return []
    path = Path(path_value).expanduser()
    if path.suffix.lower() not in {".json", ".jsonl", ".ndjson"} or not path.is_file() or path.is_symlink():
        return []
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(max(0, size - max_bytes))
            raw = handle.read(max_bytes)
    except OSError:
        return []
    text = raw.decode("utf-8", errors="ignore")
    messages: list[tuple[str, str]] = []
    if path.suffix.lower() == ".json" and size <= max_bytes:
        try:
            messages.extend(_walk_messages(json.loads(text)))
        except json.JSONDecodeError:
            return []
    else:
        for line in text.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            messages.extend(_walk_messages(payload))
    turns = []
    seen: set[tuple[str, str]] = set()
    for role, content in messages:
        clean = clean_text(content, turn_tokens)
        key = (role, clean)
        if clean and key not in seen:
            turns.append({"role": role, "text": clean})
            seen.add(key)
    return turns


def checkpoint(
    root: Path,
    project: str,
    task_id: str,
    *,
    trigger: str,
    transcript_path: str | None = None,
    goal: str | None = None,
    decisions: Iterable[str] = (),
    open_loops: Iterable[str] = (),
    artifacts: Iterable[str] = (),
    next_action: str | None = None,
    memory_ids: Iterable[str] = (),
    include_usage_memories: bool = True,
) -> dict[str, Any]:
    project = canonical_segment(project, "project")
    task_id = canonical_segment(task_id, "task")
    config = load_config(root)
    settings = config.get("continuity", {})
    turn_limit = int(settings.get("turn_tokens", 400))
    recent_limit = int(settings.get("recent_turns", 6))
    extracted = transcript_turns(
        transcript_path,
        max_bytes=int(settings.get("transcript_tail_bytes", 1048576)),
        turn_tokens=turn_limit,
    )
    task_events = (
        [event for event in load_events(root) if str(event.get("task_id", "")) == task_id]
        if include_usage_memories
        else []
    )
    used_ids = [
        str(event["memory_id"])
        for event in task_events
        if event.get("event") == "memory_used" and event.get("memory_id")
    ]
    with DataLock(root):
        capsule = load_capsule(root, project, task_id)
        if goal:
            capsule["goal"] = clean_text(goal, turn_limit)
        capsule["decisions"] = unique(
            [*capsule.get("decisions", []), *(clean_text(item, turn_limit) for item in decisions if item)],
            20,
        )
        capsule["open_loops"] = unique(
            [*capsule.get("open_loops", []), *(clean_text(item, turn_limit) for item in open_loops if item)],
            20,
        )
        capsule["artifacts"] = unique(
            [*capsule.get("artifacts", []), *(safe_artifact(item) for item in artifacts if item)], 30
        )
        if next_action:
            capsule["next_action"] = clean_text(next_action, turn_limit)
        turns = [
            {
                "role": item["role"],
                "text": item["text"],
                "event_key": event_id("transcript_turn", task_id, item["role"], item["text"]),
                "time": now_iso(root),
            }
            for item in extracted
        ]
        capsule["recent_turns"] = unique([*capsule.get("recent_turns", []), *turns], recent_limit)
        if not capsule.get("goal"):
            first_user = next((item["text"] for item in capsule["recent_turns"] if item["role"] == "user"), "")
            capsule["goal"] = first_user
        capsule["memory_ids"] = unique([*capsule.get("memory_ids", []), *used_ids, *memory_ids], 30)
        state = load_state(root, project)
        capsule["watermark"] = {
            "event_seq": int(state.get("last_event_seq", 0)),
            "checkpoint_seq": int(state.get("last_checkpoint_seq", 0)),
            "checkpoint_token_offset": int(state.get("checkpoint_token_offset", 0)),
            "unconsolidated_tokens": int(state.get("unconsolidated_tokens", 0)),
        }
        capsule["last_trigger"] = trigger
        if trigger in {"PreCompact", "before_compact", "compact"}:
            capsule["compression_count"] = int(capsule.get("compression_count", 0)) + 1
        path = _write_capsule_unlocked(root, capsule)
        checkpoint_id = event_id("continuity_checkpoint", task_id, capsule["revision"])
        append_event(
            root,
            {
                "event": "continuity_checkpoint",
                "event_id": checkpoint_id,
                "project": project,
                "task_id": task_id,
                "trigger": trigger,
                "capsule_revision": capsule["revision"],
                "watermark_seq": capsule["watermark"]["event_seq"],
                "path": path.relative_to(root).as_posix(),
            },
            assume_locked=True,
        )
    return {
        "checkpoint_id": checkpoint_id,
        "path": path.relative_to(root).as_posix(),
        "revision": capsule["revision"],
        "watermark": capsule["watermark"],
        "compression_count": capsule["compression_count"],
    }


def relevant_index_entries(root: Path, capsule: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    payload = read_json(contained_path(root, "index.json"), {}) or {}
    entries = [entry for entry in payload.get("entries", []) if entry.get("project") == capsule.get("project")]
    explicit = list(capsule.get("memory_ids", []))
    ordered = [entry for memory_id in explicit for entry in entries if entry.get("id") == memory_id]
    query = f"{capsule.get('goal', '')} {capsule.get('next_action', '')}".lower()
    query_terms = set(re.findall(r"[A-Za-z0-9_.-]+|[\u3400-\u9fff]", query))
    scored = []
    for entry in entries:
        haystack = " ".join(
            [str(entry.get("title", "")), " ".join(entry.get("tags", [])), str(entry.get("category", ""))]
        ).lower()
        score = sum(term in haystack for term in query_terms)
        if score:
            scored.append((score, entry))
    ordered.extend(entry for _, entry in sorted(scored, key=lambda item: (item[0], item[1]["id"]), reverse=True))
    result = []
    seen = set()
    for entry in ordered:
        if entry.get("id") not in seen:
            result.append(entry)
            seen.add(entry.get("id"))
        if len(result) >= limit:
            break
    return result


def resume_context(root: Path, project: str, task_id: str) -> dict[str, Any]:
    capsule = load_capsule(root, project, task_id)
    config = load_config(root)
    limit = int(config.get("continuity", {}).get("injection_tokens", 1200))
    memories = relevant_index_entries(root, capsule, 3)
    lines = [
        "[Engineering Memory Continuity Capsule]",
        f"Same task: {capsule.get('task_id')} | project: {capsule.get('project')} | revision: {capsule.get('revision')}",
        "Continue this task in place. Do not ask the user to restate context unless an unresolved ambiguity is listed.",
    ]
    if capsule.get("goal"):
        lines.append(f"Goal: {capsule['goal']}")
    if capsule.get("decisions"):
        lines.append("Decisions: " + " | ".join(capsule["decisions"][-8:]))
    if capsule.get("open_loops"):
        lines.append("Open loops: " + " | ".join(capsule["open_loops"][-8:]))
    if capsule.get("artifacts"):
        lines.append("Artifacts: " + ", ".join(capsule["artifacts"][-12:]))
    if capsule.get("next_action"):
        lines.append(f"Next action: {capsule['next_action']}")
    if memories:
        lines.append(
            "Memory refs (open only if needed, max 3): "
            + " | ".join(f"{item['id']} {item['title']} @ {item['path']}" for item in memories)
        )
    if capsule.get("recent_turns"):
        lines.append(
            "Recent compressed turns: "
            + " | ".join(f"{item['role']}: {item['text']}" for item in capsule["recent_turns"][-4:])
        )
    context = clean_context("\n".join(lines), limit)
    return {
        "task_id": capsule.get("task_id"),
        "project": capsule.get("project"),
        "revision": capsule.get("revision", 0),
        "compression_count": capsule.get("compression_count", 0),
        "estimated_tokens": estimate_tokens(context),
        "limit_tokens": limit,
        "context": context,
        "memory_ids": [item.get("id") for item in memories],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("checkpoint", "capture", "resume", "show"))
    parser.add_argument("--root", help="data directory; defaults to the Skill's data/")
    parser.add_argument("--project", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--transcript-path")
    parser.add_argument("--goal")
    parser.add_argument("--decision", action="append", default=[])
    parser.add_argument("--open-loop", action="append", default=[])
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--next-action")
    parser.add_argument("--memory-id", action="append", default=[])
    args = parser.parse_args(argv)
    root = resolve_data_root(args.root)
    try:
        if args.command == "resume":
            result = resume_context(root, args.project, args.task_id)
        elif args.command == "show":
            result = load_capsule(root, args.project, args.task_id)
        else:
            result = checkpoint(
                root,
                args.project,
                args.task_id,
                trigger=args.trigger,
                transcript_path=args.transcript_path,
                goal=args.goal,
                decisions=args.decision,
                open_loops=args.open_loop,
                artifacts=args.artifact,
                next_action=args.next_action,
                memory_ids=args.memory_id,
            )
        print_json(result)
        return 0
    except (MemoryErrorBase, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
