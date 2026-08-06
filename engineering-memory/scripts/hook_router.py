#!/usr/bin/env python3
"""Route Codex or WorkBuddy-like lifecycle events into Engineering Memory."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from _memory_common import (
    MemoryErrorBase,
    append_event,
    canonical_segment,
    detect_project,
    estimate_tokens,
    event_id,
    load_events,
    load_state,
    print_json,
    receipt_for_task,
    receipt_text,
    resolve_data_root,
    task_finalized,
)
from adapters import adapter_for
from context_continuity import checkpoint, record_turn, resume_context
from maintenance_queue import enqueue_consolidation, spawn_worker


RECEIPT_MARKER = "记忆备份："


def project_identity(event: Any) -> str:
    if event.project:
        return canonical_segment(str(event.project), "project")
    raw = event.raw
    cwd = raw.get("cwd") or raw.get("workspace") or raw.get("working_directory")
    return detect_project(str(cwd) if cwd else None)


def queue_if_due(root: Path, project: str, task_id: str) -> dict[str, Any]:
    state = load_state(root, project)
    if state.get("maintenance_due") != "due":
        return {
            "queued": False,
            "due": state.get("maintenance_due", "no"),
            "watermark_seq": int(state.get("last_event_seq", 0)),
            "worker": {"spawned": False, "reason": "not_due"},
        }
    queued = enqueue_consolidation(
        root,
        project,
        task_id=task_id,
        watermark_seq=int(state.get("last_event_seq", 0)),
    )
    worker = spawn_worker(root, project=project)
    return {
        "queued": queued["queued"],
        "due": "due",
        "job_id": queued["job_id"],
        "job_state": queued["state"],
        "watermark_seq": queued["watermark_seq"],
        "worker": worker,
    }


def handle(payload: dict[str, Any], root: Path, host: str = "codex") -> dict[str, Any]:
    adapter = adapter_for(host)
    event = adapter.normalize(payload)
    name = event.name
    task_id = canonical_segment(event.task_id, "task")
    project = project_identity(event)
    base_id = event.event_id or ""

    if name == "SessionStart":
        append_event(
            root,
            {
                "event": "hook_session_start",
                "event_id": base_id or event_id("hook_session_start", task_id, event.source),
                "project": project,
                "task_id": task_id,
                "source": event.source or "startup",
                "host": event.host,
            },
        )
        if (event.source or "startup") in {"compact", "resume", "startup"}:
            restored = resume_context(root, project, task_id)
            if restored["revision"] or event.source == "compact":
                return adapter.render(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": restored["context"],
                        },
                        "engineering_memory": {
                            "continuity": {
                                key: value for key, value in restored.items() if key != "context"
                            }
                        },
                    }
                )
        return adapter.render({})

    if name == "UserPromptSubmit":
        prompt = event.prompt
        record_turn(root, project, task_id, role="user", text=prompt, event_key=base_id or None)
        append_event(
            root,
            {
                "event": "hook_user_prompt",
                "event_id": base_id or event_id("hook_user_prompt", task_id, prompt),
                "project": project,
                "task_id": task_id,
                "host": event.host,
                "estimated_prompt_tokens": estimate_tokens(prompt),
            },
            activity_tokens=estimate_tokens(prompt),
        )
        state = load_state(root, project)
        due_note = (
            " 项目活动已达 128K；Stop 只落水位线并排队，maintenance_worker.py 在后台整理余量。"
            if state.get("maintenance_due") == "due"
            else ""
        )
        context = (
            f"Engineering Memory 已无感触发。项目={project}，任务={task_id}。"
            "先查热层，必要时冷查，最多打开 3 篇；只有实际使用才更新 CAT。"
            "结束前调用记录代理并输出以“记忆备份：”开头的收据。"
            + due_note
        )
        return adapter.render(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )

    if name == "PreCompact":
        captured = checkpoint(
            root,
            project,
            task_id,
            trigger="PreCompact",
            transcript_path=event.transcript_path,
        )
        append_event(
            root,
            {
                "event": "hook_pre_compact",
                "event_id": base_id or event_id("PreCompact", task_id, captured["revision"]),
                "project": project,
                "task_id": task_id,
                "trigger": event.trigger or "auto",
                "checkpoint_id": captured["checkpoint_id"],
            },
        )
        return adapter.render(
            {
                "continue": True,
                "suppressOutput": True,
                "systemMessage": (
                    f"Engineering Memory continuity checkpoint {captured['checkpoint_id']} saved "
                    f"at revision {captured['revision']}."
                ),
                "engineering_memory": {"continuity": captured},
            }
        )

    if name == "PostCompact":
        append_event(
            root,
            {
                "event": "hook_post_compact",
                "event_id": base_id or event_id("PostCompact", task_id, event.trigger),
                "project": project,
                "task_id": task_id,
                "trigger": event.trigger or "auto",
            },
        )
        return adapter.render(
            {
                "continue": True,
                "suppressOutput": True,
                "systemMessage": "Continuity capsule is ready for SessionStart(source=compact) restoration.",
            }
        )

    if name in {"SubagentStart", "SubagentStop"}:
        append_event(
            root,
            {
                "event": "hook_subagent_start" if name == "SubagentStart" else "hook_subagent_stop",
                "event_id": base_id
                or event_id(name, task_id, event.agent_id, event.assistant_output),
                "project": project,
                "task_id": task_id,
                "agent_id": event.agent_id,
                "agent_type": event.agent_type,
                "host": event.host,
            },
            activity_tokens=estimate_tokens(event.assistant_output) if name == "SubagentStop" else 0,
        )
        is_recorder = "engineering_memory_recorder" in str(event.agent_type or "")
        if (
            name == "SubagentStop"
            and is_recorder
            and RECEIPT_MARKER not in event.assistant_output
            and not event.stop_hook_active
        ):
            return adapter.render(
                {
                    "decision": "block",
                    "reason": "记录代理必须输出以‘记忆备份：’开头的完整备份描述后才能结束。",
                }
            )
        return adapter.render({})

    if name == "Stop":
        started = time.perf_counter()
        output = event.assistant_output
        record_turn(root, project, task_id, role="assistant", text=output, event_key=base_id or None)
        append_event(
            root,
            {
                "event": "hook_stop_observed",
                "event_id": base_id or event_id("hook_stop_observed", task_id, output),
                "project": project,
                "task_id": task_id,
                "host": event.host,
                "has_receipt": RECEIPT_MARKER in output,
            },
            activity_tokens=estimate_tokens(output),
        )
        captured = checkpoint(
            root,
            project,
            task_id,
            trigger="Stop",
            include_usage_memories=False,
        )
        maintenance = queue_if_due(root, project, task_id)
        maintenance["stop_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        maintenance["checkpoint_id"] = captured["checkpoint_id"]
        events = load_events(root)
        if event.stop_hook_active or RECEIPT_MARKER in output or task_finalized(events, task_id):
            return adapter.render(
                {
                    "engineering_memory": {
                        "status": "allowed",
                        "maintenance": maintenance,
                        "continuity": captured,
                    }
                }
            )
        reason = (
            "请调用 engineering_memory_recorder 完成收尾：只沉淀五类工程事实；"
            "短任务写 ≤300 token 候选并询问‘锁定 / 不锁定 / 修改后锁定’；"
            "随后 finalize，并输出以‘记忆备份：’开头的完整收据。"
        )
        return adapter.render({"decision": "block", "reason": reason})

    if name == "SessionEnd":
        maintenance = queue_if_due(root, project, task_id)
        append_event(
            root,
            {
                "event": "hook_session_end",
                "event_id": base_id or event_id("hook_session_end", task_id),
                "project": project,
                "task_id": task_id,
                "host": event.host,
                "maintenance_job_id": maintenance.get("job_id"),
            },
        )
        return adapter.render({"engineering_memory": {"maintenance": maintenance}})

    append_event(
        root,
        {
            "event": "hook_unknown",
            "event_id": base_id or event_id("hook_unknown", name, task_id),
            "project": project,
            "task_id": task_id,
            "host": event.host,
            "hook_name": name,
        },
    )
    return adapter.render({})


def finalize(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    project = canonical_segment(args.project, "project")
    task_id = canonical_segment(args.task_id, "task")
    receipt = args.receipt or receipt_text(receipt_for_task(root, task_id, project))
    append_event(
        root,
        {
            "event": "task_finalized",
            "event_id": event_id("task_finalized", task_id),
            "project": project,
            "task_id": task_id,
            "receipt": receipt,
        },
    )
    return {"task_id": task_id, "project": project, "receipt": receipt, "finalized": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("handle", "normalize", "finalize"), default="handle")
    parser.add_argument("--root", help="data directory; defaults to the Skill's data/")
    parser.add_argument("--host", choices=("codex", "workbuddy", "generic"), default="codex")
    parser.add_argument("--task-id")
    parser.add_argument("--project")
    parser.add_argument("--receipt")
    args = parser.parse_args(argv)
    root = resolve_data_root(args.root)
    try:
        if args.command == "finalize":
            if not args.task_id or not args.project:
                raise MemoryErrorBase("finalize requires --task-id and --project")
            print_json(finalize(args, root))
            return 0
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise MemoryErrorBase("hook input must be a JSON object")
        adapter = adapter_for(args.host)
        if args.command == "normalize":
            print_json(adapter.normalize(payload).to_dict())
            return 0
        try:
            result = handle(payload, root, args.host)
        except Exception as exc:
            result = adapter.render({"engineering_memory": {"status": "fail_open", "error": str(exc)}})
        print_json(result)
        return 0
    except (MemoryErrorBase, ValueError, json.JSONDecodeError, OSError) as exc:
        print_json({"engineering_memory": {"status": "fail_open", "error": str(exc)}})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
