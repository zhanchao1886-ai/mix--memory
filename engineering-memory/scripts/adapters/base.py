"""Host-neutral event contract and adapter selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class NormalizedEvent:
    host: str
    name: str
    task_id: str
    project: str | None
    event_id: str | None
    prompt: str
    assistant_output: str
    transcript_path: str | None
    source: str | None
    trigger: str | None
    stop_hook_active: bool
    agent_id: str | None
    agent_type: str | None
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Adapter(Protocol):
    name: str

    def normalize(self, payload: dict[str, Any]) -> NormalizedEvent: ...

    def render(self, result: dict[str, Any]) -> dict[str, Any]: ...


def adapter_for(name: str) -> Adapter:
    normalized = (name or "codex").strip().lower().replace("_", "-")
    if normalized == "codex":
        from .codex import CodexAdapter

        return CodexAdapter()
    if normalized in {"workbuddy", "work-buddy", "generic"}:
        from .workbuddy import WorkBuddyAdapter

        return WorkBuddyAdapter(name="generic" if normalized == "generic" else "workbuddy")
    raise ValueError(f"unsupported host adapter: {name}")
