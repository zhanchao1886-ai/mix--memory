#!/usr/bin/env python3
"""Search the hot index first, then fall back to the full cold index."""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from _memory_common import (
    MemoryErrorBase,
    append_event,
    canonical_segment,
    cat_rank,
    contained_path,
    estimate_tokens,
    event_id,
    load_config,
    print_json,
    read_json,
    redact_sensitive,
    resolve_data_root,
)


def terms(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            token.lower()
            for token in re.findall(r"[A-Za-z0-9_./-]+|[\u3400-\u9fff]", text)
            if token.strip()
        )
    )


def relevance(entry: dict[str, Any], query: str, wanted_tags: list[str]) -> float:
    query_lower = query.strip().lower()
    title = str(entry.get("title", "")).lower()
    memory_id = str(entry.get("id", "")).lower()
    tags = [str(tag).lower() for tag in entry.get("tags", [])]
    path = str(entry.get("path", "")).lower()
    score = 0.0
    if query_lower and query_lower == memory_id:
        score += 1000
    if query_lower and query_lower in title:
        score += 80
    if query_lower and query_lower in tags:
        score += 60
    for token in terms(query):
        if token == memory_id:
            score += 60
        if token in title:
            score += 12
        if token in tags:
            score += 10
        if token in path:
            score += 2
    for tag in wanted_tags:
        score += 30 if tag in tags else 0
    if score > 0:
        score += max(0, cat_rank(str(entry.get("cat", "")))) * 0.75
    return score


def search(
    payload: dict[str, Any], query: str, project: str | None, wanted_tags: list[str], limit: int
) -> tuple[list[dict[str, Any]], str]:
    entries = payload.get("entries", [])
    hot_ids = set(payload.get("hot_ids", []))
    if project:
        entries = [entry for entry in entries if entry.get("project") == project]

    def ranked(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scored = []
        for entry in pool:
            score = relevance(entry, query, wanted_tags)
            if score > 0:
                item = dict(entry)
                item["score"] = round(score, 3)
                scored.append(item)
        return sorted(
            scored,
            key=lambda item: (float(item["score"]), cat_rank(str(item.get("cat", ""))), item["id"]),
            reverse=True,
        )

    hot = ranked([entry for entry in entries if entry.get("id") in hot_ids])
    if len(hot) >= limit:
        return hot[:limit], "hot"
    all_ranked = ranked(entries)
    return all_ranked[:limit], "cold" if any(item.get("id") not in hot_ids for item in all_ranked[:limit]) else "hot"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--root", help="data directory; defaults to the Skill's data/")
    parser.add_argument("--project")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--log-usage", action="store_true")
    parser.add_argument("--task-id", default="search-task")
    args = parser.parse_args(argv)
    root = resolve_data_root(args.root)
    try:
        config = load_config(root)
        maximum = int(config.get("limits", {}).get("candidate_limit", 3))
        limit = max(1, min(args.limit, maximum))
        project = canonical_segment(args.project, "project") if args.project else None
        wanted_tags = [canonical_segment(tag, "tag").lower() for tag in args.tag]
        payload = read_json(contained_path(root, "index.json"), {}) or {}
        results, layer = search(payload, args.query, project, wanted_tags, limit)
        if args.log_usage:
            append_event(
                root,
                {
                    "event": "memories_retrieved",
                    "event_id": event_id(
                        "memories_retrieved",
                        args.task_id,
                        project,
                        args.query,
                        ",".join(item["id"] for item in results),
                    ),
                    "project": project,
                    "task_id": args.task_id,
                    "query": redact_sensitive(args.query),
                    "tags": wanted_tags,
                    "retrieved_ids": [item["id"] for item in results],
                    "layer": layer,
                },
                activity_tokens=estimate_tokens(args.query),
            )
        print_json(
            {
                "query": args.query,
                "project": project,
                "layer": layer,
                "limit": limit,
                "count": len(results),
                "results": results,
            }
        )
        return 0
    except (MemoryErrorBase, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
