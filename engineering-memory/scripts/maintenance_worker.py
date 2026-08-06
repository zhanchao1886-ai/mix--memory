#!/usr/bin/env python3
"""Drain Engineering Memory maintenance jobs outside foreground Stop hooks."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _memory_common import MemoryErrorBase, load_config, print_json, resolve_data_root
from consolidate_project import consolidate
from maintenance_queue import claim_next_job, finish_job, queue_counts, recover_stale_jobs


def run_job(root, job: dict[str, Any]) -> dict[str, Any]:
    kind = job.get("kind")
    if kind != "consolidate_project":
        raise MemoryErrorBase(f"unsupported maintenance job: {kind}")
    return consolidate(
        root,
        str(job["project"]),
        watermark_seq=int(job["watermark_seq"]),
    )


def drain(root, *, project: str | None = None, max_jobs: int | None = None) -> dict[str, Any]:
    config = load_config(root)
    configured = int(config.get("background", {}).get("max_jobs_per_worker", 4))
    limit = max(1, int(max_jobs if max_jobs is not None else configured))
    recovered = recover_stale_jobs(root, project)
    completed: list[dict[str, Any]] = []
    for _ in range(limit):
        job = claim_next_job(root, project)
        if job is None:
            break
        try:
            result = run_job(root, job)
            final = finish_job(root, job, success=True, result=result)
        except Exception as exc:  # keep worker alive for other independent jobs
            final = finish_job(root, job, success=False, error=str(exc))
        completed.append(
            {
                "job_id": final.get("job_id"),
                "state": final.get("state"),
                "attempts": final.get("attempts"),
            }
        )
    return {
        "processed": len(completed),
        "recovered": recovered,
        "jobs": completed,
        "queue": queue_counts(root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="data directory; defaults to the Skill's data/")
    parser.add_argument("--project")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--max-jobs", type=int)
    args = parser.parse_args(argv)
    root = resolve_data_root(args.root)
    try:
        maximum = 1 if args.once else args.max_jobs
        result = drain(root, project=args.project, max_jobs=maximum)
        print_json(result)
        return 0
    except (MemoryErrorBase, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
