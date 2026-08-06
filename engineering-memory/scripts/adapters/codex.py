"""Codex hook wire-format adapter."""

from __future__ import annotations

from typing import Any

from .base import NormalizedEvent


class CodexAdapter:
    name = "codex"

    def normalize(self, payload: dict[str, Any]) -> NormalizedEvent:
        name = str(
            payload.get("hook_event_name")
            or payload.get("hookEventName")
            or payload.get("event_name")
            or payload.get("event")
            or "Unknown"
        )
        task_id = str(
            payload.get("session_id")
            or payload.get("thread_id")
            or payload.get("task_id")
            or payload.get("conversation_id")
            or "hook-task"
        )
        return NormalizedEvent(
            host=self.name,
            name=name,
            task_id=task_id,
            project=str(payload.get("project") or payload.get("project_name") or "") or None,
            event_id=str(payload.get("hook_event_id") or payload.get("event_id") or "") or None,
            prompt=str(payload.get("prompt") or payload.get("user_prompt") or payload.get("message") or ""),
            assistant_output=str(
                payload.get("last_assistant_message")
                or payload.get("assistant_message")
                or payload.get("response")
                or payload.get("output")
                or ""
            ),
            transcript_path=str(payload.get("transcript_path") or "") or None,
            source=str(payload.get("source") or "") or None,
            trigger=str(payload.get("trigger") or "") or None,
            stop_hook_active=bool(payload.get("stop_hook_active") or payload.get("stopHookActive")),
            agent_id=str(payload.get("agent_id") or payload.get("subagent_id") or "") or None,
            agent_type=str(payload.get("agent_type") or payload.get("subagent_type") or "") or None,
            raw=payload,
        )

    def render(self, result: dict[str, Any]) -> dict[str, Any]:
        return result
