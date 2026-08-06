#!/usr/bin/env python3
"""Apply CAT transitions, log metrics and finalize a task receipt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from _memory_common import (
    DataLock,
    MemoryErrorBase,
    append_event,
    budget_result,
    canonical_segment,
    cat_rank,
    contained_path,
    event_id,
    iter_memory_paths,
    load_config,
    load_events,
    make_cat,
    now_iso,
    parse_cat,
    print_json,
    read_memory,
    rebuild_indexes,
    receipt_for_task,
    receipt_text,
    redact_sensitive,
    resolve_data_root,
    semantic_hash,
    today,
    used_task_ids,
    write_memory,
)


def find_memory(root: Path, memory_id: str, project: str | None = None) -> Path:
    for path in iter_memory_paths(root, project):
        try:
            meta, _, _ = read_memory(path)
        except (OSError, MemoryErrorBase):
            continue
        if meta.get("id") == memory_id:
            return path
    raise MemoryErrorBase(f"memory not found: {memory_id}")


def reset_memory(
    root: Path,
    path: Path,
    meta: dict[str, Any],
    body: str,
    *,
    task_id: str,
    reason: str,
    dry_run: bool,
) -> dict[str, Any]:
    old_cat = str(meta.get("cat"))
    new_meta = dict(meta)
    new_meta["cat"] = make_cat("unobserved", 0, today(root))
    new_meta["updated"] = now_iso(root)
    new_meta["content_hash"] = semantic_hash(new_meta, body)
    result = {
        "memory_id": meta.get("id"),
        "from": old_cat,
        "to": new_meta["cat"],
        "reason": reason,
        "dry_run": dry_run,
    }
    if not dry_run:
        write_memory(root, new_meta, body, preserve_hash=True)
        append_event(
            root,
            {
                "event": "memory_reset",
                "event_id": event_id("memory_reset", meta.get("id"), task_id, reason, new_meta["content_hash"]),
                "project": meta.get("project"),
                "task_id": task_id,
                "memory_id": meta.get("id"),
                "reason": reason,
                "from_state": parse_cat(old_cat)[0],
                "to_state": "unobserved",
            },
            assume_locked=True,
        )
    return result


def metrics(root: Path, task_id: str) -> dict[str, Any]:
    events = load_events(root)
    task = [event for event in events if str(event.get("task_id", "")) == task_id]
    retrieved: list[str] = []
    for event in task:
        if event.get("event") == "memories_retrieved":
            retrieved.extend(str(item) for item in event.get("retrieved_ids", []))
    retrieved_unique = list(dict.fromkeys(retrieved))
    used = list(
        dict.fromkeys(
            str(event.get("memory_id"))
            for event in task
            if event.get("event") == "memory_used" and event.get("memory_id")
        )
    )
    utilized = len(set(retrieved_unique) & set(used))
    utilization = utilized / len(retrieved_unique) if retrieved_unique else None
    cats = []
    for path in iter_memory_paths(root):
        try:
            meta, _, errors = read_memory(path)
        except (OSError, MemoryErrorBase):
            continue
        if not errors:
            cats.append(cat_rank(str(meta.get("cat", ""))))
    stable_share = sum(rank == 2 for rank in cats) / len(cats) if cats else None
    total_used_events = sum(event.get("event") == "memory_used" for event in events)
    total_conflicts = sum(
        event.get("event") == "memory_reset" and event.get("reason", "").startswith("conflict")
        for event in events
    )
    contradiction_rate = total_conflicts / total_used_events if total_used_events else None
    return {
        "retrieved": len(retrieved_unique),
        "used": len(used),
        "candidate_utilization": utilization,
        "target_candidate_utilization": 0.8,
        "stable_share": stable_share,
        "contradiction_rate": contradiction_rate,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="data directory; defaults to the Skill's data/")
    parser.add_argument("--project")
    parser.add_argument("--task-id", default="mark-task")
    parser.add_argument("--used", action="append", default=[])
    parser.add_argument("--conflict", action="append", default=[])
    parser.add_argument("--reason", default="conflict: reported by task")
    parser.add_argument("--scan", action="store_true", help="reset memories whose semantic hash changed")
    parser.add_argument("--pipeline-tokens", type=int)
    parser.add_argument("--task-total-tokens", type=int)
    parser.add_argument("--measurement", choices=("exact", "estimated"), default="estimated")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = resolve_data_root(args.root)
    project = canonical_segment(args.project, "project") if args.project else None
    config = load_config(root)
    changes: list[dict[str, Any]] = []
    touched: set[str] = set()

    try:
        lock = DataLock(root) if not args.dry_run else None
        context = lock if lock is not None else __import__("contextlib").nullcontext()
        with context:
            if args.scan:
                for path in iter_memory_paths(root, project):
                    meta, body, errors = read_memory(path)
                    if "content_hash mismatch" in errors:
                        changes.append(
                            reset_memory(
                                root,
                                path,
                                meta,
                                body,
                                task_id=args.task_id,
                                reason="content_modified",
                                dry_run=args.dry_run,
                            )
                        )
                        touched.add(str(meta["id"]))

            for memory_id in args.conflict:
                path = find_memory(root, memory_id, project)
                meta, body, _ = read_memory(path)
                changes.append(
                    reset_memory(
                        root,
                        path,
                        meta,
                        body,
                        task_id=args.task_id,
                        reason=(
                            redact_sensitive(args.reason)
                            if args.reason.startswith("conflict")
                            else f"conflict: {redact_sensitive(args.reason)}"
                        ),
                        dry_run=args.dry_run,
                    )
                )
                touched.add(memory_id)

            for memory_id in args.used:
                path = find_memory(root, memory_id, project)
                meta, body, errors = read_memory(path)
                virtually_reset = False
                if "content_hash mismatch" in errors:
                    changes.append(
                        reset_memory(
                            root,
                            path,
                            meta,
                            body,
                            task_id=args.task_id,
                            reason="content_modified",
                            dry_run=args.dry_run,
                        )
                    )
                    meta, body, _ = read_memory(path) if not args.dry_run else (dict(meta), body, [])
                    if args.dry_run:
                        meta["cat"] = make_cat("unobserved", 0, today(root))
                        virtually_reset = True
                    touched.add(memory_id)
                events = load_events(root)
                prior_tasks = set() if virtually_reset else used_task_ids(events, memory_id)
                if args.task_id in prior_tasks:
                    changes.append({"memory_id": memory_id, "transition": "duplicate_task_ignored"})
                    continue
                new_count = len(prior_tasks) + 1
                threshold = int(config.get("limits", {}).get("stable_ref_count", 2))
                new_state = "stable" if new_count >= threshold else "observed"
                old_cat = str(meta.get("cat"))
                new_cat = make_cat(new_state, new_count, today(root))
                changes.append(
                    {"memory_id": memory_id, "from": old_cat, "to": new_cat, "dry_run": args.dry_run}
                )
                touched.add(memory_id)
                if not args.dry_run:
                    meta["cat"] = new_cat
                    meta["updated"] = now_iso(root)
                    write_memory(root, meta, body, preserve_hash=True)
                    append_event(
                        root,
                        {
                            "event": "memory_used",
                            "event_id": event_id("memory_used", memory_id, args.task_id),
                            "project": meta.get("project"),
                            "task_id": args.task_id,
                            "memory_id": memory_id,
                            "from_state": parse_cat(old_cat)[0],
                            "to_state": new_state,
                            "ref_count": new_count,
                        },
                        assume_locked=True,
                    )

            budget = None
            if args.pipeline_tokens is not None or args.task_total_tokens is not None:
                if args.pipeline_tokens is None or args.task_total_tokens is None:
                    raise MemoryErrorBase("both --pipeline-tokens and --task-total-tokens are required")
                budget = budget_result(args.pipeline_tokens, args.task_total_tokens, config)
                budget["measurement"] = args.measurement
                if not args.dry_run:
                    append_event(
                        root,
                        {
                            "event": "budget_recorded",
                            "event_id": event_id(
                                "budget_recorded",
                                args.task_id,
                                args.pipeline_tokens,
                                args.task_total_tokens,
                            ),
                            "project": project,
                            "task_id": args.task_id,
                            "budget": budget,
                        },
                        assume_locked=True,
                    )

            index_result = None
            if touched and not args.dry_run:
                for memory_id in sorted(touched):
                    index_result = rebuild_indexes(root, changed_id=memory_id)
                append_event(
                    root,
                    {
                        "event": "index_rebuilt",
                        "event_id": event_id("index_rebuilt", args.task_id, ",".join(sorted(touched))),
                        "project": project,
                        "task_id": args.task_id,
                        "mode": "incremental",
                        "result": index_result,
                    },
                    assume_locked=True,
                )

            task_metrics = metrics(root, args.task_id)
            if not args.dry_run:
                append_event(
                    root,
                    {
                        "event": "metrics_recorded",
                        "event_id": event_id("metrics_recorded", args.task_id, len(load_events(root))),
                        "project": project,
                        "task_id": args.task_id,
                        "metrics": task_metrics,
                    },
                    assume_locked=True,
                )
            receipt = receipt_for_task(root, args.task_id, project or "project")
            text = receipt_text(receipt)
            if args.finalize and not args.dry_run:
                append_event(
                    root,
                    {
                        "event": "task_finalized",
                        "event_id": event_id("task_finalized", args.task_id),
                        "project": project,
                        "task_id": args.task_id,
                        "receipt": text,
                    },
                    assume_locked=True,
                )

        print_json(
            {
                "task_id": args.task_id,
                "project": project,
                "dry_run": args.dry_run,
                "changes": changes,
                "budget": budget,
                "metrics": task_metrics,
                "index": index_result,
                "receipt": text,
            }
        )
        return 0
    except (MemoryErrorBase, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
