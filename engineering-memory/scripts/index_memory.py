#!/usr/bin/env python3
"""Rebuild the hot Markdown and full JSON indexes from formal memories."""

from __future__ import annotations

import argparse
import sys

from _memory_common import (
    DataLock,
    MemoryErrorBase,
    append_event,
    canonical_segment,
    contained_path,
    event_id,
    load_events,
    print_json,
    read_json,
    rebuild_indexes,
    resolve_data_root,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="data directory; defaults to the Skill's data/")
    parser.add_argument("--changed-id", help="incrementally refresh one memory ID")
    parser.add_argument("--project", help="project used for the audit event")
    parser.add_argument("--task-id", default="index-maintenance")
    parser.add_argument("--check", action="store_true", help="validate existing index without writing")
    args = parser.parse_args(argv)
    root = resolve_data_root(args.root)
    try:
        if args.check:
            payload = read_json(contained_path(root, "index.json"), {}) or {}
            missing = []
            for entry in payload.get("entries", []):
                path = contained_path(root, str(entry.get("path", "")))
                if not path.is_file():
                    missing.append(str(entry.get("id")))
            result = {
                "valid": not missing,
                "entries": len(payload.get("entries", [])),
                "missing_paths": missing,
                "events": len(load_events(root)),
            }
            print_json(result)
            return 0 if result["valid"] else 1
        with DataLock(root):
            result = rebuild_indexes(root, changed_id=args.changed_id)
            project = canonical_segment(args.project, "project") if args.project else None
            append_event(
                root,
                {
                    "event": "index_rebuilt",
                    "event_id": event_id(
                        "index_rebuilt", args.task_id, args.changed_id or "full", result["entries"]
                    ),
                    "project": project,
                    "task_id": args.task_id,
                    "mode": "incremental" if args.changed_id else "full",
                    "result": result,
                },
                assume_locked=True,
            )
        print_json(result)
        return 0
    except (MemoryErrorBase, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
