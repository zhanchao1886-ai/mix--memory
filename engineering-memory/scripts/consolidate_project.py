#!/usr/bin/env python3
"""Consolidate a project's deferred candidates at the 128K watermark."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _memory_common import (
    DataLock,
    MemoryErrorBase,
    append_event,
    candidate_statuses,
    canonical_segment,
    event_id,
    load_config,
    load_events,
    load_state,
    make_cat,
    make_memory_id,
    now_iso,
    print_json,
    project_state_path,
    rebuild_indexes,
    resolve_data_root,
    today,
    write_memory,
    atomic_write_json,
)


def consolidation_plan(
    root,
    project: str,
    force: bool = False,
    watermark_seq: int | None = None,
) -> dict[str, Any]:
    config = load_config(root)
    state = load_state(root, project)
    settings = config.get("consolidation", {})
    hard = int(settings.get("hard_tokens", 131072))
    capacity = int(settings.get("chunk_tokens", 16384)) * int(settings.get("max_chunks_per_run", 8))
    events = [event for event in load_events(root) if event.get("project") == project]
    checkpoint = int(state.get("last_checkpoint_seq", 0))
    starting_offset = int(state.get("checkpoint_token_offset", 0))
    requested_watermark = int(watermark_seq) if watermark_seq is not None else None
    pending = [
        event
        for event in events
        if int(event.get("seq", 0)) > checkpoint
        and (requested_watermark is None or int(event.get("seq", 0)) <= requested_watermark)
    ]
    target_seq = checkpoint
    target_offset = starting_offset
    processed_tokens = 0
    for event in pending:
        cost = max(0, int(event.get("activity_tokens", 0)))
        seq = int(event.get("seq", checkpoint))
        already_consumed = starting_offset if seq == checkpoint + 1 else 0
        remaining_cost = max(0, cost - already_consumed)
        available = max(0, capacity - processed_tokens)
        taken = min(remaining_cost, available)
        processed_tokens += taken
        if taken == remaining_cost:
            target_seq = seq
            target_offset = 0
        else:
            target_offset = already_consumed + taken
            break
        if processed_tokens >= capacity:
            break
    due = int(state.get("unconsolidated_tokens", 0)) >= hard
    statuses = candidate_statuses(events)
    candidates = [
        item
        for item in statuses.values()
        if item.get("status") == "deferred" and int(item.get("seq", 0)) <= target_seq
    ]
    return {
        "project": project,
        "due": due,
        "force": force,
        "will_run": (force or due) and bool(pending),
        "requested_watermark_seq": requested_watermark,
        "watermark_seq": target_seq,
        "previous_checkpoint_seq": checkpoint,
        "previous_checkpoint_token_offset": starting_offset,
        "checkpoint_token_offset": target_offset,
        "processed_tokens": processed_tokens,
        "remaining_tokens": max(0, int(state.get("unconsolidated_tokens", 0)) - processed_tokens),
        "candidate_ids": [item["candidate_id"] for item in candidates],
        "capacity_tokens": capacity,
    }


def consolidate(
    root,
    project: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    watermark_seq: int | None = None,
) -> dict[str, Any]:
    project = canonical_segment(project, "project")
    plan = consolidation_plan(root, project, force, watermark_seq)
    if dry_run or not plan["will_run"]:
        return {**plan, "dry_run": dry_run, "promoted": [], "index": None}

    promoted: list[dict[str, str]] = []
    with DataLock(root):
        # Recompute after taking the lock so the checkpoint and candidate set agree.
        plan = consolidation_plan(root, project, force, watermark_seq)
        if not plan["will_run"]:
            return {**plan, "dry_run": False, "promoted": [], "index": None}
        state = load_state(root, project)
        state["consolidation_running"] = True
        state["updated"] = now_iso(root)
        atomic_write_json(project_state_path(root, project), state, root)
        try:
            statuses = candidate_statuses(load_events(root))
            for candidate_id in plan["candidate_ids"]:
                item = statuses.get(candidate_id)
                if not item or item.get("status") != "deferred":
                    continue
                memory_id = make_memory_id(root, project, str(item["title"]), str(item["content"]))
                timestamp = now_iso(root)
                meta = {
                    "id": memory_id,
                    "title": item["title"],
                    "project": project,
                    "tags": item.get("tags", []),
                    "category": item["category"],
                    "source": item.get("source", f"candidate:{candidate_id}"),
                    "created": timestamp,
                    "updated": timestamp,
                    "cat": make_cat("unobserved", 0, today(root)),
                }
                path = write_memory(root, meta, str(item["content"]))
                append_event(
                    root,
                    {
                        "event": "candidate_promoted",
                        "event_id": event_id("candidate_promoted", candidate_id, memory_id),
                        "project": project,
                        "task_id": "128k-consolidation",
                        "candidate_id": candidate_id,
                        "memory_id": memory_id,
                        "path": path.relative_to(root).as_posix(),
                    },
                    assume_locked=True,
                )
                promoted.append({"candidate_id": candidate_id, "memory_id": memory_id})
            index_result = rebuild_indexes(root)
            # Checkpoint advances only after memories and both indexes succeeded.
            state = load_state(root, project)
            state["last_checkpoint_seq"] = plan["watermark_seq"]
            state["checkpoint_token_offset"] = plan["checkpoint_token_offset"]
            state["unconsolidated_tokens"] = plan["remaining_tokens"]
            config = load_config(root)
            hard = int(config.get("consolidation", {}).get("hard_tokens", 131072))
            soft = int(config.get("consolidation", {}).get("soft_tokens", 122880))
            if state["unconsolidated_tokens"] >= hard:
                state["maintenance_due"] = "due"
            elif state["unconsolidated_tokens"] >= soft:
                state["maintenance_due"] = "soon"
            else:
                state["maintenance_due"] = "no"
            state["consolidation_running"] = False
            state["updated"] = now_iso(root)
            atomic_write_json(project_state_path(root, project), state, root)
            append_event(
                root,
                {
                    "event": "consolidation_completed",
                    "event_id": event_id(
                        "consolidation_completed", project, plan["watermark_seq"], plan["processed_tokens"]
                    ),
                    "project": project,
                    "task_id": "128k-consolidation",
                    "watermark_seq": plan["watermark_seq"],
                    "processed_tokens": plan["processed_tokens"],
                    "remaining_tokens": plan["remaining_tokens"],
                    "promoted_ids": [item["memory_id"] for item in promoted],
                    "maintenance_budget": "separate",
                },
                assume_locked=True,
            )
        except Exception:
            state = load_state(root, project)
            state["consolidation_running"] = False
            state["updated"] = now_iso(root)
            atomic_write_json(project_state_path(root, project), state, root)
            raise
    return {**plan, "dry_run": False, "promoted": promoted, "index": index_result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="data directory; defaults to the Skill's data/")
    parser.add_argument("--project", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--watermark-seq", type=int)
    args = parser.parse_args(argv)
    root = resolve_data_root(args.root)
    try:
        result = consolidate(
            root,
            args.project,
            force=args.force,
            dry_run=args.dry_run,
            watermark_seq=args.watermark_seq,
        )
        print_json(result)
        return 0
    except (MemoryErrorBase, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
