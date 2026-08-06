#!/usr/bin/env python3
"""Persistent, portable maintenance queue for work that must not block Stop."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from _memory_common import (
    DataLock,
    MemoryErrorBase,
    append_event,
    atomic_write_json,
    canonical_segment,
    contained_path,
    event_id,
    load_config,
    load_state,
    now,
    now_iso,
    project_state_path,
    read_json,
    redact_sensitive,
)


JOB_STATES = ("pending", "running", "done", "failed")


def job_path(root: Path, state: str, job_id: str) -> Path:
    if state not in JOB_STATES:
        raise MemoryErrorBase(f"invalid job state: {state}")
    return contained_path(root, Path("jobs") / state / f"{canonical_segment(job_id, 'job')}.json")


def locate_job(root: Path, job_id: str) -> tuple[str, Path] | None:
    for state in JOB_STATES:
        path = job_path(root, state, job_id)
        if path.exists():
            return state, path
    return None


def enqueue_consolidation(
    root: Path,
    project: str,
    *,
    task_id: str,
    watermark_seq: int | None = None,
) -> dict[str, Any]:
    """Enqueue one idempotent consolidation job and return without doing heavy work."""
    project = canonical_segment(project, "project")
    task_id = canonical_segment(task_id, "task")
    with DataLock(root):
        state = load_state(root, project)
        active_id = str(state.get("maintenance_job_id") or "")
        active = locate_job(root, active_id) if active_id else None
        if active and active[0] in {"pending", "running"}:
            existing = read_json(active[1], {}) or {}
            return {
                "job_id": active_id,
                "state": active[0],
                "queued": False,
                "watermark_seq": int(existing.get("watermark_seq", 0)),
                "job": existing,
            }
        watermark = int(watermark_seq if watermark_seq is not None else state.get("last_event_seq", 0))
        job_id = "JOB-" + event_id("consolidate", project, watermark).removeprefix("EV-")
        located = locate_job(root, job_id)
        if located:
            existing = read_json(located[1], {}) or {}
            return {
                "job_id": job_id,
                "state": located[0],
                "queued": False,
                "watermark_seq": watermark,
                "job": existing,
            }
        payload = {
            "schema_version": 1,
            "job_id": job_id,
            "kind": "consolidate_project",
            "project": project,
            "task_id": task_id,
            "watermark_seq": watermark,
            "created": now_iso(root),
            "updated": now_iso(root),
            "attempts": 0,
            "state": "pending",
        }
        atomic_write_json(job_path(root, "pending", job_id), payload, root)
        state["maintenance_job_id"] = job_id
        state["last_stop_watermark_seq"] = watermark
        state["updated"] = now_iso(root)
        atomic_write_json(project_state_path(root, project), state, root)
        append_event(
            root,
            {
                "event": "maintenance_queued",
                "event_id": event_id("maintenance_queued", job_id),
                "project": project,
                "task_id": task_id,
                "job_id": job_id,
                "watermark_seq": watermark,
            },
            assume_locked=True,
        )
        return {
            "job_id": job_id,
            "state": "pending",
            "queued": True,
            "watermark_seq": watermark,
            "job": payload,
        }


def claim_next_job(root: Path, project: str | None = None) -> dict[str, Any] | None:
    wanted = canonical_segment(project, "project") if project else None
    with DataLock(root):
        pending_dir = contained_path(root, "jobs/pending")
        for path in sorted(pending_dir.glob("*.json")):
            payload = read_json(path, {}) or {}
            if wanted and payload.get("project") != wanted:
                continue
            job_id = str(payload.get("job_id") or path.stem)
            running = job_path(root, "running", job_id)
            try:
                os.replace(path, running)
            except FileNotFoundError:
                continue
            payload["state"] = "running"
            payload["attempts"] = int(payload.get("attempts", 0)) + 1
            payload["started"] = now_iso(root)
            payload["updated"] = payload["started"]
            lease_seconds = max(0, int(load_config(root).get("background", {}).get("lease_seconds", 900)))
            payload["lease_expires"] = (
                now(root) + dt.timedelta(seconds=lease_seconds)
            ).replace(microsecond=0).isoformat()
            payload["claim_token"] = event_id(
                "maintenance_claim",
                job_id,
                payload["attempts"],
                payload["started"],
                os.getpid(),
            )
            payload["worker_pid"] = os.getpid()
            atomic_write_json(running, payload, root)
            append_event(
                root,
                {
                    "event": "maintenance_started",
                    "event_id": event_id("maintenance_started", job_id, payload["attempts"]),
                    "project": payload.get("project"),
                    "task_id": payload.get("task_id"),
                    "job_id": job_id,
                    "attempt": payload["attempts"],
                },
                assume_locked=True,
            )
            return payload
    return None


def recover_stale_jobs(root: Path, project: str | None = None) -> list[str]:
    """Return expired running leases to pending after a worker crash."""
    wanted = canonical_segment(project, "project") if project else None
    recovered: list[str] = []
    with DataLock(root):
        current = now(root)
        running_dir = contained_path(root, "jobs/running")
        for path in sorted(running_dir.glob("*.json")):
            payload = read_json(path, {}) or {}
            if wanted and payload.get("project") != wanted:
                continue
            try:
                expires = dt.datetime.fromisoformat(str(payload.get("lease_expires", "")))
            except ValueError:
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=current.tzinfo)
            if expires > current:
                continue
            job_id = str(payload.get("job_id") or path.stem)
            payload["state"] = "pending"
            payload["updated"] = now_iso(root)
            payload["recovered_after_lease"] = True
            for key in ("started", "lease_expires", "claim_token", "worker_pid"):
                payload.pop(key, None)
            atomic_write_json(job_path(root, "pending", job_id), payload, root)
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            append_event(
                root,
                {
                    "event": "maintenance_recovered",
                    "event_id": event_id("maintenance_recovered", job_id, payload.get("attempts")),
                    "project": payload.get("project"),
                    "task_id": payload.get("task_id"),
                    "job_id": job_id,
                },
                assume_locked=True,
            )
            recovered.append(job_id)
    return recovered


def finish_job(
    root: Path,
    job: dict[str, Any],
    *,
    success: bool,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    job_id = str(job["job_id"])
    with DataLock(root):
        running = job_path(root, "running", job_id)
        current = read_json(running, None)
        if not isinstance(current, dict):
            located = locate_job(root, job_id)
            if located:
                return read_json(located[1], job) or dict(job)
            current = dict(job)
        if job.get("claim_token") and current.get("claim_token") != job.get("claim_token"):
            return {**dict(job), "state": "lease_lost", "updated": now_iso(root)}
        payload = current
        config = load_config(root)
        maximum = int(config.get("background", {}).get("max_attempts", 3))
        retry = not success and int(payload.get("attempts", 0)) < maximum
        target_state = "pending" if retry else ("done" if success else "failed")
        payload["state"] = target_state
        payload["updated"] = now_iso(root)
        payload["finished"] = now_iso(root) if not retry else None
        if result is not None:
            payload["result"] = result
        if error:
            payload["error"] = redact_sensitive(str(error))[:1000]
        target = job_path(root, target_state, job_id)
        atomic_write_json(target, payload, root)
        with contextlib.suppress(FileNotFoundError):
            running.unlink()
        project = str(payload.get("project") or "")
        if project:
            state = load_state(root, project)
            if state.get("maintenance_job_id") == job_id:
                state["maintenance_job_id"] = job_id if retry else None
                state["updated"] = now_iso(root)
                atomic_write_json(project_state_path(root, project), state, root)
        append_event(
            root,
            {
                "event": "maintenance_requeued" if retry else ("maintenance_completed" if success else "maintenance_failed"),
                "event_id": event_id("maintenance_finish", job_id, payload.get("attempts"), target_state),
                "project": payload.get("project"),
                "task_id": payload.get("task_id"),
                "job_id": job_id,
                "job_state": target_state,
            },
            assume_locked=True,
        )
        return payload


def queue_counts(root: Path) -> dict[str, int]:
    return {
        state: len(list(contained_path(root, Path("jobs") / state).glob("*.json")))
        for state in JOB_STATES
    }


def spawn_worker(root: Path, *, project: str | None = None) -> dict[str, Any]:
    """Start a detached worker; never wait for consolidation in the hook process."""
    config = load_config(root)
    settings = config.get("background", {})
    mode = str(settings.get("mode", "spawn"))
    if mode != "spawn" or not bool(settings.get("auto_spawn", True)):
        return {"spawned": False, "mode": mode, "reason": "manual_background_mode"}
    worker = Path(__file__).resolve().parent / "maintenance_worker.py"
    command = [sys.executable, str(worker), "--root", str(root), "--drain"]
    if project:
        command.extend(["--project", canonical_segment(project, "project")])
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI when available
        kwargs["creationflags"] = int(getattr(subprocess, "DETACHED_PROCESS", 0)) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        return {"spawned": False, "mode": mode, "reason": redact_sensitive(str(exc))}
    return {"spawned": True, "mode": mode, "pid": process.pid}
