"""Generic bridge for WorkBuddy-like persistent-task runtimes.

This maps a documented public interchange envelope, not a private vendor API.
Hosts can emit either the aliases below or the canonical Codex lifecycle names.
"""

from __future__ import annotations

from typing import Any

from .base import NormalizedEvent


ALIASES = {
    "message_submitted": "UserPromptSubmit",
    "user_prompt": "UserPromptSubmit",
    "turn_stop": "Stop",
    "turn_complete": "Stop",
    "before_compact": "PreCompact",
    "context_before_compact": "PreCompact",
    "after_compact": "PostCompact",
    "context_after_compact": "PostCompact",
    "task_resume": "SessionStart",
    "session_resume": "SessionStart",
    "task_start": "SessionStart",
    "task_end": "SessionEnd",
    "subagent_start": "SubagentStart",
    "subagent_stop": "SubagentStop",
}


def nested(payload: dict[str, Any], *path: str) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


class WorkBuddyAdapter:
    def __init__(self, name: str = "workbuddy") -> None:
        self.name = name

    def normalize(self, payload: dict[str, Any]) -> NormalizedEvent:
        raw_name = str(payload.get("event") or payload.get("event_type") or payload.get("type") or "Unknown")
        name = ALIASES.get(raw_name, raw_name)
        task_id = str(
            payload.get("task_id")
            or payload.get("conversation_id")
            or nested(payload, "task", "id")
            or nested(payload, "conversation", "id")
            or "workbuddy-task"
        )
        source = str(payload.get("source") or payload.get("resume_source") or "") or None
        if name == "SessionStart" and not source:
            source = "compact" if raw_name in {"task_resume", "session_resume"} else "startup"
        return NormalizedEvent(
            host=self.name,
            name=name,
            task_id=task_id,
            project=str(
                payload.get("project")
                or payload.get("project_name")
                or nested(payload, "task", "project")
                or ""
            )
            or None,
            event_id=str(payload.get("event_id") or payload.get("idempotency_key") or "") or None,
            prompt=str(payload.get("prompt") or payload.get("message") or nested(payload, "input", "text") or ""),
            assistant_output=str(
                payload.get("assistant_output")
                or payload.get("response")
                or nested(payload, "output", "text")
                or ""
            ),
            transcript_path=str(payload.get("transcript_path") or payload.get("conversation_log") or "") or None,
            source=source,
            trigger=str(payload.get("trigger") or payload.get("compaction_trigger") or "") or None,
            stop_hook_active=bool(payload.get("continuation_active") or payload.get("stop_hook_active")),
            agent_id=str(payload.get("agent_id") or nested(payload, "agent", "id") or "") or None,
            agent_type=str(payload.get("agent_type") or nested(payload, "agent", "type") or "") or None,
            raw=payload,
        )

    def render(self, result: dict[str, Any]) -> dict[str, Any]:
        specific = result.get("hookSpecificOutput", {}) if isinstance(result, dict) else {}
        additional = specific.get("additionalContext") or result.get("systemMessage")
        rendered = {
            "protocol": "engineering-memory.host-event.v1",
            "continue": result.get("decision") != "block" and result.get("continue", True) is not False,
            "decision": result.get("decision", "allow"),
            "reason": result.get("reason"),
            "additional_context": additional,
            "engineering_memory": result.get("engineering_memory", {}),
        }
        return {key: value for key, value in rendered.items() if value is not None}
