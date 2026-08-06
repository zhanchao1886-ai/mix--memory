#!/usr/bin/env python3
"""Create a locked engineering memory or a short deferred candidate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _memory_common import (
    CATEGORIES,
    DataLock,
    MemoryErrorBase,
    append_event,
    candidate_statuses,
    canonical_segment,
    contained_path,
    estimate_tokens,
    event_id,
    load_config,
    load_events,
    make_candidate_id,
    make_cat,
    make_memory_id,
    now_iso,
    parse_tags,
    print_json,
    rebuild_indexes,
    receipt_for_task,
    receipt_text,
    redact_sensitive,
    resolve_data_root,
    today,
    write_memory,
)


def parser() -> argparse.ArgumentParser:
    item = argparse.ArgumentParser(description=__doc__)
    item.add_argument("--root", help="data directory; defaults to the Skill's data/")
    item.add_argument("--project", required=True)
    item.add_argument("--title")
    item.add_argument("--category", choices=sorted(CATEGORIES))
    item.add_argument("--tags", default="")
    item.add_argument("--content")
    item.add_argument("--source")
    item.add_argument("--task-id", default="manual-task")
    item.add_argument("--mode", choices=("candidate", "locked", "rejected"), default="candidate")
    item.add_argument("--candidate-id")
    item.add_argument("--memory-id")
    return item


def require(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise MemoryErrorBase(f"{name} is required for this mode")
    return value.strip()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = resolve_data_root(args.root)
    project = canonical_segment(args.project, "project")
    task_id = args.task_id.strip() or "manual-task"
    config = load_config(root)

    try:
        if args.mode == "rejected":
            candidate_id = require(args.candidate_id, "--candidate-id")
            statuses = candidate_statuses(load_events(root))
            if candidate_id not in statuses:
                raise MemoryErrorBase(f"unknown candidate: {candidate_id}")
            event, created = append_event(
                root,
                {
                    "event": "candidate_rejected",
                    "event_id": event_id("candidate_rejected", candidate_id),
                    "project": project,
                    "task_id": task_id,
                    "candidate_id": candidate_id,
                },
            )
            result = {"status": "rejected", "created": created, "event": event}
            result["receipt"] = receipt_text(receipt_for_task(root, task_id, project))
            print_json(result)
            return 0

        candidate = None
        if args.candidate_id:
            candidate = candidate_statuses(load_events(root)).get(args.candidate_id)
            if candidate is None:
                raise MemoryErrorBase(f"unknown candidate: {args.candidate_id}")
            if candidate.get("status") == "rejected":
                raise MemoryErrorBase("a rejected candidate cannot be promoted")

        title = redact_sensitive(args.title or (candidate or {}).get("title", "")).strip()
        category = args.category or (candidate or {}).get("category")
        tags = parse_tags(args.tags) if args.tags else list((candidate or {}).get("tags", []))
        content = redact_sensitive(args.content or (candidate or {}).get("content", "")).strip()
        source = redact_sensitive(
            args.source or (candidate or {}).get("source", f"task:{task_id}")
        ).strip()
        title = require(title, "--title")
        content = require(content, "--content")
        if category not in CATEGORIES:
            raise MemoryErrorBase("--category must be supplied and use the five-category enum")

        if args.mode == "candidate":
            limit = int(config.get("limits", {}).get("short_memory_tokens", 300))
            count = estimate_tokens(content)
            if count > limit:
                raise MemoryErrorBase(
                    f"candidate is {count} estimated tokens; limit is {limit}; use --mode locked or shorten it"
                )
            candidate_id = args.candidate_id or make_candidate_id(project, task_id, title, content)
            event, created = append_event(
                root,
                {
                    "event": "candidate_created",
                    "event_id": event_id("candidate_created", candidate_id),
                    "project": project,
                    "task_id": task_id,
                    "candidate_id": candidate_id,
                    "title": title,
                    "category": category,
                    "tags": tags,
                    "content": content,
                    "source": source,
                    "estimated_tokens": count,
                },
                activity_tokens=count,
            )
            result = {
                "status": "candidate",
                "created": created,
                "candidate_id": candidate_id,
                "event": event,
            }
            result["receipt"] = receipt_text(receipt_for_task(root, task_id, project))
            result["lock_prompt"] = "是否锁定本次记忆备份？可回复：锁定 / 不锁定 / 修改后锁定。"
            print_json(result)
            return 0

        timestamp = now_iso(root)
        memory_id = args.memory_id or make_memory_id(root, project, title, content)
        meta = {
            "id": canonical_segment(memory_id, "id"),
            "title": title,
            "project": project,
            "tags": tags,
            "category": category,
            "source": source,
            "created": timestamp,
            "updated": timestamp,
            "cat": make_cat("unobserved", 0, today(root)),
        }
        with DataLock(root):
            path = contained_path(root, Path("projects") / project / "memories" / f"{meta['id']}.md")
            existed = path.exists()
            if not existed:
                path = write_memory(root, meta, content)
            memory_event, event_created = append_event(
                root,
                {
                    "event": "memory_created",
                    "event_id": event_id("memory_created", meta["id"]),
                    "project": project,
                    "task_id": task_id,
                    "memory_id": meta["id"],
                    "path": path.relative_to(root).as_posix(),
                    "candidate_id": args.candidate_id,
                },
                activity_tokens=estimate_tokens(content) if not existed else 0,
                assume_locked=True,
            )
            if args.candidate_id:
                append_event(
                    root,
                    {
                        "event": "candidate_locked",
                        "event_id": event_id("candidate_locked", args.candidate_id, meta["id"]),
                        "project": project,
                        "task_id": task_id,
                        "candidate_id": args.candidate_id,
                        "memory_id": meta["id"],
                    },
                    assume_locked=True,
                )
            index_result = rebuild_indexes(root, changed_id=meta["id"])
            append_event(
                root,
                {
                    "event": "index_rebuilt",
                    "event_id": event_id("index_rebuilt", task_id, meta["id"]),
                    "project": project,
                    "task_id": task_id,
                    "mode": "incremental",
                    "result": index_result,
                },
                assume_locked=True,
            )
        result = {
            "status": "locked",
            "created": event_created,
            "memory_id": meta["id"],
            "path": path.relative_to(root).as_posix(),
            "event": memory_event,
            "index": index_result,
            "receipt": receipt_text(receipt_for_task(root, task_id, project)),
        }
        print_json(result)
        return 0
    except MemoryErrorBase as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
