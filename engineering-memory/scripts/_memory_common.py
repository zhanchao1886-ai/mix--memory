#!/usr/bin/env python3
"""Shared primitives for the portable engineering-memory skill.

Runtime callers are intentionally constrained to the supplied data root.  The
global installer is the sole exception and does not use these write helpers.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 is unsupported
    ZoneInfo = None


CATEGORIES = {"decision", "outcome", "lesson", "glossary", "filemap"}
CAT_STATES = {"unobserved", "observed", "stable"}
CAT_RE = re.compile(r"^CAT:(unobserved|observed|stable):(\d+):(\d{4}-\d{2}-\d{2})$")
ABSOLUTE_PATH_RE = re.compile(
    r"(?<![:/\w.-])(?:/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+|[A-Za-z]:[\\/][^\s`'\"]+)"
)
SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd)\b"
        r"\s*[:=]\s*([^\s,;]+)"
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END [^-]+-----"),
)
FRONTMATTER_ORDER = (
    "id",
    "title",
    "project",
    "tags",
    "category",
    "source",
    "created",
    "updated",
    "cat",
    "content_hash",
)
_EVENT_ID_CACHE: dict[str, tuple[int, int, set[str], dict[str, dict[str, Any]]]] = {}


class MemoryErrorBase(RuntimeError):
    """Actionable error raised by memory scripts."""


class DataLock:
    """Cross-process lock scoped to a data root."""

    def __init__(self, root: Path):
        self.root = root
        self.handle = None

    def __enter__(self) -> "DataLock":
        ensure_layout(self.root)
        lock_path = contained_path(self.root, ".engineering-memory.lock")
        self.handle = lock_path.open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is not None and fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        if self.handle is not None:
            self.handle.close()


def default_data_root() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def resolve_data_root(value: str | os.PathLike[str] | None = None) -> Path:
    root = Path(value).expanduser() if value else default_data_root()
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def contained_path(root: Path, relative: str | os.PathLike[str]) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise MemoryErrorBase(f"unsafe relative path: {relative}")
    target = root.joinpath(rel)
    resolved_parent = target.parent.resolve()
    try:
        resolved_parent.relative_to(root.resolve())
    except ValueError as exc:
        raise MemoryErrorBase(f"path escapes data root: {relative}") from exc
    if target.exists() and target.is_symlink():
        resolved = target.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise MemoryErrorBase(f"symlink escapes data root: {relative}") from exc
    return target


def ensure_layout(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    contained_path(root, "projects").mkdir(parents=True, exist_ok=True)
    for relative in (
        "jobs/pending",
        "jobs/running",
        "jobs/done",
        "jobs/failed",
        "runtime",
    ):
        contained_path(root, relative).mkdir(parents=True, exist_ok=True)
    defaults = {
        "config.json": {
            "schema_version": 2,
            "timezone": "Asia/Shanghai",
            "projects": [],
            "limits": {
                "hot_index_tokens": 2000,
                "candidate_limit": 3,
                "short_memory_tokens": 300,
                "pipeline_soft_ratio": 0.25,
                "pipeline_hard_ratio": 0.30,
                "stable_ref_count": 2,
            },
            "consolidation": {
                "soft_tokens": 122880,
                "hard_tokens": 131072,
                "chunk_tokens": 16384,
                "max_chunks_per_run": 8,
            },
            "background": {
                "mode": "spawn",
                "auto_spawn": True,
                "stop_budget_ms": 250,
                "max_jobs_per_worker": 4,
                "max_attempts": 3,
                "lease_seconds": 900,
            },
            "continuity": {
                "enabled": True,
                "injection_tokens": 1200,
                "recent_turns": 6,
                "turn_tokens": 400,
                "transcript_tail_bytes": 1048576,
            },
            "host": {"default_adapter": "codex", "portable": True},
            "global_trigger": {"enabled": True, "require_receipt": True},
        },
        "index.json": {"schema_version": 1, "generated_at": None, "hot_ids": [], "entries": []},
    }
    for name, payload in defaults.items():
        path = contained_path(root, name)
        if not path.exists():
            atomic_write_json(path, payload, root)
    log_path = contained_path(root, "usage-log.jsonl")
    if not log_path.exists():
        atomic_write_text(log_path, "", root)


def atomic_write_text(path: Path, text: str, root: Path) -> None:
    path = contained_path(root, path.relative_to(root) if path.is_absolute() else path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def atomic_write_json(path: Path, payload: Any, root: Path) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", root)


def append_json_line(path: Path, payload: Any, root: Path) -> None:
    """Durably append one JSONL record; callers must hold the data lock."""
    path = contained_path(root, path.relative_to(root) if path.is_absolute() else path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    needs_separator = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as reader:
            reader.seek(-1, os.SEEK_END)
            needs_separator = reader.read(1) != b"\n"
    with path.open("ab") as handle:
        if needs_separator:
            handle.write(b"\n")
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _event_index_paths(root: Path) -> tuple[Path, Path, Path]:
    return (
        contained_path(root, "usage-log.jsonl"),
        contained_path(root, "runtime/event-ids.jsonl"),
        contained_path(root, "runtime/event-index.json"),
    )


def _rebuild_event_id_index(
    root: Path,
) -> tuple[int, int, set[str], dict[str, dict[str, Any]]]:
    log_path, ids_path, meta_path = _event_index_paths(root)
    events = load_events(root)
    identifiers = {str(item.get("event_id")) for item in events if item.get("event_id")}
    text = "".join(json.dumps(identifier, ensure_ascii=False) + "\n" for identifier in sorted(identifiers))
    atomic_write_text(ids_path, text, root)
    stat = log_path.stat()
    atomic_write_json(
        meta_path,
        {"schema_version": 1, "log_size": stat.st_size, "log_mtime_ns": stat.st_mtime_ns},
        root,
    )
    return stat.st_size, stat.st_mtime_ns, identifiers, {
        str(item["event_id"]): item for item in events if item.get("event_id")
    }


def _load_event_id_index(
    root: Path,
) -> tuple[int, int, set[str], dict[str, dict[str, Any]]]:
    log_path, ids_path, meta_path = _event_index_paths(root)
    stat = log_path.stat()
    key = str(root.resolve())
    cached = _EVENT_ID_CACHE.get(key)
    if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
        return cached
    meta = read_json(meta_path, {}) or {}
    if (
        ids_path.is_file()
        and int(meta.get("log_size", -1)) == stat.st_size
        and int(meta.get("log_mtime_ns", -1)) == stat.st_mtime_ns
    ):
        try:
            identifiers = {
                str(json.loads(line))
                for line in ids_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        except (json.JSONDecodeError, OSError):
            indexed = _rebuild_event_id_index(root)
        else:
            indexed = (stat.st_size, stat.st_mtime_ns, identifiers, {})
    else:
        indexed = _rebuild_event_id_index(root)
    _EVENT_ID_CACHE[key] = indexed
    return indexed


def _record_event_id(root: Path, payload: dict[str, Any]) -> None:
    log_path, ids_path, meta_path = _event_index_paths(root)
    append_json_line(ids_path, str(payload["event_id"]), root)
    stat = log_path.stat()
    atomic_write_json(
        meta_path,
        {"schema_version": 1, "log_size": stat.st_size, "log_mtime_ns": stat.st_mtime_ns},
        root,
    )
    key = str(root.resolve())
    cached = _EVENT_ID_CACHE.get(key)
    identifiers = set(cached[2]) if cached else set()
    recent = dict(cached[3]) if cached else {}
    identifiers.add(str(payload["event_id"]))
    recent[str(payload["event_id"])] = payload
    _EVENT_ID_CACHE[key] = (stat.st_size, stat.st_mtime_ns, identifiers, recent)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MemoryErrorBase(f"invalid JSON: {path.name}: {exc}") from exc


def load_config(root: Path) -> dict[str, Any]:
    ensure_layout(root)
    config = read_json(contained_path(root, "config.json"), {})
    if not isinstance(config, dict):
        raise MemoryErrorBase("config.json must contain an object")
    return config


def now(root: Path | None = None) -> dt.datetime:
    timezone = "Asia/Shanghai"
    if root is not None:
        config = read_json(contained_path(root, "config.json"), {}) or {}
        timezone = str(config.get("timezone", timezone))
    if ZoneInfo is not None:
        try:
            return dt.datetime.now(ZoneInfo(timezone))
        except Exception:
            pass
    return dt.datetime.now(dt.timezone.utc)


def now_iso(root: Path | None = None) -> str:
    return now(root).replace(microsecond=0).isoformat()


def today(root: Path | None = None) -> str:
    return now(root).date().isoformat()


def canonical_segment(value: str, kind: str = "path") -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    if not normalized or normalized in {".", ".."}:
        raise MemoryErrorBase(f"empty or unsafe {kind}")
    cleaned = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE).strip(".-")
    if not cleaned or cleaned in {".", ".."}:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
        cleaned = f"{kind}-{digest}"
    return cleaned[:100]


def detect_project(cwd: str | os.PathLike[str] | None = None) -> str:
    location = Path(cwd or os.getcwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(location), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        location = Path(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return canonical_segment(location.name or "project", "project")


def estimate_tokens(text: str) -> int:
    """Conservative dependency-free estimator for budget guards."""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u3000-\u30ff\u3400-\u9fff\uf900-\ufaff]", text))
    remainder = re.sub(r"[\u3000-\u30ff\u3400-\u9fff\uf900-\ufaff]", " ", text)
    ascii_chunks = re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]", remainder)
    ascii_cost = sum(max(1, math.ceil(len(chunk) / 4)) for chunk in ascii_chunks)
    return cjk + ascii_cost


def redact_sensitive(text: str) -> str:
    clean = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            clean = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", clean)
        else:
            clean = pattern.sub("[REDACTED]", clean)
    clean = ABSOLUTE_PATH_RE.sub(
        lambda match: match.group(0).rstrip(".,;)").replace("\\", "/").rsplit("/", 1)[-1]
        or "[PATH]",
        clean,
    )
    return clean


def parse_tags(value: str | Iterable[str]) -> list[str]:
    raw = value.split(",") if isinstance(value, str) else list(value)
    tags: list[str] = []
    for item in raw:
        tag = canonical_segment(str(item).strip(), "tag").lower()
        if tag not in tags:
            tags.append(tag)
    return tags[:20]


def parse_cat(value: str) -> tuple[str, int, str]:
    match = CAT_RE.fullmatch(value)
    if not match:
        raise MemoryErrorBase(f"invalid CAT marker: {value}")
    return match.group(1), int(match.group(2)), match.group(3)


def make_cat(state: str, ref_count: int, check_date: str) -> str:
    if state not in CAT_STATES or ref_count < 0:
        raise MemoryErrorBase("invalid CAT state or reference count")
    return f"CAT:{state}:{ref_count}:{check_date}"


def semantic_hash(meta: dict[str, Any], body: str) -> str:
    payload = {
        "id": meta.get("id"),
        "title": meta.get("title"),
        "project": meta.get("project"),
        "tags": meta.get("tags", []),
        "category": meta.get("category"),
        "source": meta.get("source"),
        "body": body.strip(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise MemoryErrorBase("memory is missing YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise MemoryErrorBase("memory frontmatter is not closed")
    meta: dict[str, Any] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition(":")
        if not sep:
            raise MemoryErrorBase(f"invalid frontmatter line: {line}")
        key = key.strip()
        raw = raw.strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        meta[key] = value
    return meta, text[end + 5 :].strip() + "\n"


def dump_frontmatter(meta: dict[str, Any], body: str) -> str:
    keys = [key for key in FRONTMATTER_ORDER if key in meta]
    keys.extend(sorted(key for key in meta if key not in FRONTMATTER_ORDER))
    lines = ["---"]
    for key in keys:
        lines.append(f"{key}: {json.dumps(meta[key], ensure_ascii=False)}")
    lines.extend(["---", "", body.strip(), ""])
    return "\n".join(lines)


def validate_memory(meta: dict[str, Any], body: str, *, verify_hash: bool = True) -> list[str]:
    errors: list[str] = []
    required = set(FRONTMATTER_ORDER)
    missing = sorted(required - set(meta))
    if missing:
        errors.append("missing fields: " + ", ".join(missing))
    if meta.get("category") not in CATEGORIES:
        errors.append(f"invalid category: {meta.get('category')}")
    try:
        parse_cat(str(meta.get("cat", "")))
    except MemoryErrorBase as exc:
        errors.append(str(exc))
    if not isinstance(meta.get("tags"), list):
        errors.append("tags must be a list")
    for field in ("id", "project"):
        value = str(meta.get(field, ""))
        try:
            if canonical_segment(value, field) != value:
                errors.append(f"unsafe {field}: {value}")
        except MemoryErrorBase as exc:
            errors.append(str(exc))
    if not body.strip():
        errors.append("memory body is empty")
    if verify_hash and meta.get("content_hash") and meta.get("content_hash") != semantic_hash(meta, body):
        errors.append("content_hash mismatch")
    return errors


def memory_relative_path(project: str, memory_id: str) -> Path:
    safe_project = canonical_segment(project, "project")
    safe_id = canonical_segment(memory_id, "id")
    return Path("projects") / safe_project / "memories" / f"{safe_id}.md"


def read_memory(path: Path) -> tuple[dict[str, Any], str, list[str]]:
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    return meta, body, validate_memory(meta, body)


def write_memory(root: Path, meta: dict[str, Any], body: str, *, preserve_hash: bool = False) -> Path:
    meta = dict(meta)
    meta["project"] = canonical_segment(str(meta["project"]), "project")
    meta["id"] = canonical_segment(str(meta["id"]), "id")
    meta["tags"] = parse_tags(meta.get("tags", []))
    if meta.get("category") not in CATEGORIES:
        raise MemoryErrorBase(f"category must be one of: {', '.join(sorted(CATEGORIES))}")
    body = redact_sensitive(body).strip()
    if not preserve_hash:
        meta["content_hash"] = semantic_hash(meta, body)
    errors = validate_memory(meta, body, verify_hash=True)
    if errors:
        raise MemoryErrorBase("; ".join(errors))
    relative = memory_relative_path(meta["project"], meta["id"])
    path = contained_path(root, relative)
    atomic_write_text(path, dump_frontmatter(meta, body), root)
    return path


def iter_memory_paths(root: Path, project: str | None = None) -> Iterator[Path]:
    projects_root = contained_path(root, "projects")
    if project:
        bases = [contained_path(root, Path("projects") / canonical_segment(project, "project"))]
    else:
        bases = [path for path in projects_root.iterdir() if path.is_dir()] if projects_root.exists() else []
    for base in sorted(bases):
        memories = base / "memories"
        if memories.exists():
            for path in sorted(memories.glob("*.md")):
                try:
                    path.resolve().relative_to(root.resolve())
                except ValueError:
                    continue
                yield path


def make_memory_id(root: Path, project: str, title: str, body: str) -> str:
    seed = f"{canonical_segment(project, 'project')}\0{title.strip()}\0{body.strip()}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]
    return f"EM-{now(root).strftime('%Y%m%d')}-{digest}"


def make_candidate_id(project: str, task_id: str, title: str, body: str) -> str:
    seed = f"{project}\0{task_id}\0{title}\0{body}"
    return "EMQ-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def load_events(root: Path) -> list[dict[str, Any]]:
    path = contained_path(root, "usage-log.jsonl")
    if not path.exists():
        return []
    events = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MemoryErrorBase(f"invalid usage-log line {number}: {exc}") from exc
        if isinstance(value, dict):
            events.append(value)
    return events


def event_id(event_type: str, *parts: Any) -> str:
    joined = "\0".join(str(part) for part in parts)
    digest = hashlib.sha256(f"{event_type}\0{joined}".encode("utf-8")).hexdigest()[:20]
    return f"EV-{digest}"


def project_state_path(root: Path, project: str) -> Path:
    return contained_path(root, Path("projects") / canonical_segment(project, "project") / "state.json")


def default_state(project: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project": canonical_segment(project, "project"),
        "unconsolidated_tokens": 0,
        "last_event_seq": 0,
        "last_checkpoint_seq": 0,
        "checkpoint_token_offset": 0,
        "maintenance_due": "no",
        "consolidation_running": False,
        "maintenance_job_id": None,
        "last_stop_watermark_seq": 0,
        "updated": None,
    }


def load_state(root: Path, project: str) -> dict[str, Any]:
    path = project_state_path(root, project)
    state = read_json(path, None)
    return state if isinstance(state, dict) else default_state(project)


def due_status(config: dict[str, Any], token_count: int) -> str:
    settings = config.get("consolidation", {})
    if token_count >= int(settings.get("hard_tokens", 131072)):
        return "due"
    if token_count >= int(settings.get("soft_tokens", 122880)):
        return "soon"
    return "no"


def _register_project_unlocked(root: Path, project: str) -> None:
    config_path = contained_path(root, "config.json")
    config = load_config(root)
    projects = config.setdefault("projects", [])
    if project not in projects:
        projects.append(project)
        projects.sort()
        atomic_write_json(config_path, config, root)


def append_event(
    root: Path,
    event: dict[str, Any],
    *,
    activity_tokens: int = 0,
    assume_locked: bool = False,
) -> tuple[dict[str, Any], bool]:
    lock = contextlib.nullcontext() if assume_locked else DataLock(root)
    with lock:
        ensure_layout(root)
        payload = dict(event)
        payload.setdefault("event", payload.get("type", "unknown"))
        payload.pop("type", None)
        payload.setdefault("time", now_iso(root))
        payload.setdefault("event_id", event_id(payload["event"], payload.get("task_id"), payload.get("time")))
        payload["activity_tokens"] = max(0, int(activity_tokens or payload.get("activity_tokens", 0)))
        _, _, identifiers, recent = _load_event_id_index(root)
        if payload["event_id"] in identifiers:
            existing = recent.get(str(payload["event_id"]))
            if existing is None:
                existing = next(
                    (
                        item
                        for item in reversed(load_events(root))
                        if item.get("event_id") == payload["event_id"]
                    ),
                    payload,
                )
            return existing, False
        project_raw = payload.get("project")
        if project_raw:
            project = canonical_segment(str(project_raw), "project")
            payload["project"] = project
            _register_project_unlocked(root, project)
            state = load_state(root, project)
            state["last_event_seq"] = int(state.get("last_event_seq", 0)) + 1
            state["unconsolidated_tokens"] = int(state.get("unconsolidated_tokens", 0)) + payload["activity_tokens"]
            config = load_config(root)
            state["maintenance_due"] = due_status(config, state["unconsolidated_tokens"])
            state["updated"] = payload["time"]
            payload["seq"] = state["last_event_seq"]
            atomic_write_json(project_state_path(root, project), state, root)
        append_json_line(contained_path(root, "usage-log.jsonl"), payload, root)
        _record_event_id(root, payload)
        return payload, True


def candidate_statuses(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for event in events:
        candidate_id = event.get("candidate_id")
        if not candidate_id:
            continue
        if event.get("event") == "candidate_created":
            item = dict(event)
            item["status"] = "deferred"
            statuses[candidate_id] = item
        elif candidate_id in statuses and event.get("event") in {
            "candidate_locked",
            "candidate_rejected",
            "candidate_promoted",
        }:
            statuses[candidate_id]["status"] = {
                "candidate_locked": "locked",
                "candidate_rejected": "rejected",
                "candidate_promoted": "locked",
            }[event["event"]]
            statuses[candidate_id]["status_event"] = event
    return statuses


def used_task_ids(events: Iterable[dict[str, Any]], memory_id: str) -> set[str]:
    tasks: set[str] = set()
    for event in events:
        if event.get("memory_id") != memory_id:
            continue
        if event.get("event") == "memory_reset":
            tasks.clear()
        elif event.get("event") == "memory_used" and event.get("task_id"):
            tasks.add(str(event["task_id"]))
    return tasks


def task_events(events: Iterable[dict[str, Any]], task_id: str) -> list[dict[str, Any]]:
    return [event for event in events if str(event.get("task_id", "")) == task_id]


def task_finalized(events: Iterable[dict[str, Any]], task_id: str) -> bool:
    return any(event.get("event") == "task_finalized" for event in task_events(events, task_id))


def relative_to_data(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise MemoryErrorBase(f"path is outside data root: {path}") from exc


def memory_entry(meta: dict[str, Any], path: Path, root: Path) -> dict[str, Any]:
    return {
        "id": meta["id"],
        "title": meta["title"],
        "tags": meta.get("tags", []),
        "category": meta["category"],
        "project": meta["project"],
        "cat": meta["cat"],
        "path": relative_to_data(path, root),
        "updated": meta.get("updated"),
    }


def cat_rank(cat: str) -> int:
    try:
        state, _, _ = parse_cat(cat)
    except MemoryErrorBase:
        return -1
    return {"stable": 2, "observed": 1, "unobserved": 0}[state]


def last_used_map(events: Iterable[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for event in events:
        if event.get("event") == "memory_used" and event.get("memory_id"):
            result[str(event["memory_id"])] = str(event.get("time", ""))
    return result


def escape_table(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render_hot_index(entries: list[dict[str, Any]], limit: int, events: list[dict[str, Any]]) -> tuple[str, list[str]]:
    header = (
        "# Engineering Memory Hot Index\n\n"
        "_Derived file. Rebuild with `scripts/index_memory.py`; do not edit manually._\n\n"
        "| id | title | tags | category | project | cat | path |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    recent = last_used_map(events)
    ordered = sorted(
        entries,
        key=lambda item: (
            bool(recent.get(str(item.get("id")))),
            cat_rank(str(item.get("cat", ""))),
            recent.get(str(item.get("id")), ""),
            str(item.get("updated", "")),
            str(item.get("id", "")),
        ),
        reverse=True,
    )
    text = header
    hot_ids: list[str] = []
    for entry in ordered:
        row = "| " + " | ".join(
            escape_table(entry.get(key))
            for key in ("id", "title", "tags", "category", "project", "cat", "path")
        ) + " |\n"
        if estimate_tokens(text + row) > limit:
            continue
        text += row
        hot_ids.append(str(entry["id"]))
    return text, hot_ids


def rebuild_indexes(root: Path, *, changed_id: str | None = None) -> dict[str, Any]:
    config = load_config(root)
    index_path = contained_path(root, "index.json")
    old = read_json(index_path, {}) or {}
    if changed_id:
        entries = {str(item.get("id")): item for item in old.get("entries", []) if item.get("id")}
        found = False
        for path in iter_memory_paths(root):
            try:
                meta, _, errors = read_memory(path)
            except (OSError, MemoryErrorBase):
                continue
            if meta.get("id") == changed_id:
                if errors:
                    raise MemoryErrorBase(f"cannot index {changed_id}: {'; '.join(errors)}")
                entries[changed_id] = memory_entry(meta, path, root)
                found = True
                break
        if not found:
            entries.pop(changed_id, None)
        all_entries = list(entries.values())
    else:
        all_entries = []
        seen: set[str] = set()
        for path in iter_memory_paths(root):
            meta, _, errors = read_memory(path)
            if errors:
                raise MemoryErrorBase(f"cannot index {path.name}: {'; '.join(errors)}")
            if meta["id"] in seen:
                raise MemoryErrorBase(f"duplicate memory id: {meta['id']}")
            seen.add(meta["id"])
            all_entries.append(memory_entry(meta, path, root))
    all_entries.sort(key=lambda item: (str(item.get("project")), str(item.get("id"))))
    events = load_events(root)
    hot_limit = int(config.get("limits", {}).get("hot_index_tokens", 2000))
    hot_text, hot_ids = render_hot_index(all_entries, hot_limit, events)
    payload = {
        "schema_version": 1,
        "generated_at": now_iso(root),
        "hot_ids": hot_ids,
        "entries": [
            {key: value for key, value in item.items() if key != "updated"} for item in all_entries
        ],
    }
    atomic_write_json(index_path, payload, root)
    atomic_write_text(contained_path(root, "index.md"), hot_text, root)
    return {
        "entries": len(all_entries),
        "hot_entries": len(hot_ids),
        "hot_tokens": estimate_tokens(hot_text),
        "hot_limit": hot_limit,
        "changed_id": changed_id,
    }


def budget_result(pipeline_tokens: int, task_total_tokens: int, config: dict[str, Any]) -> dict[str, Any]:
    pipeline = max(0, int(pipeline_tokens))
    total = max(0, int(task_total_tokens))
    ratio = (pipeline / total) if total else None
    limits = config.get("limits", {})
    soft = float(limits.get("pipeline_soft_ratio", 0.25))
    hard = float(limits.get("pipeline_hard_ratio", 0.30))
    if ratio is None:
        status = "unknown"
    elif ratio > hard:
        status = "hard_exceeded"
    elif ratio >= soft:
        status = "soft_guard"
    else:
        status = "within_budget"
    if status == "hard_exceeded":
        policy = {
            "mode": "minimal",
            "allow": ["necessary_search", "explicit_lock", "receipt"],
            "suppress": ["candidate_write", "cat_scan", "full_rebuild", "optional_index"],
        }
    elif status == "soft_guard":
        policy = {
            "mode": "guarded",
            "allow": ["hot_search", "cold_search_if_needed", "required_write", "receipt"],
            "suppress": ["candidate_expansion", "cat_scan", "full_rebuild"],
        }
    else:
        policy = {"mode": "normal", "allow": ["pipeline"], "suppress": []}
    return {
        "pipeline_tokens": pipeline,
        "task_total_tokens": total,
        "ratio": ratio,
        "soft_ratio": soft,
        "hard_ratio": hard,
        "status": status,
        "degraded": status in {"soft_guard", "hard_exceeded"},
        "policy": policy,
    }


def receipt_for_task(root: Path, task_id: str, project: str) -> dict[str, Any]:
    events = task_events(load_events(root), task_id)
    locked = len(
        {
            str(event.get("memory_id"))
            for event in events
            if event.get("event") == "memory_created" and event.get("memory_id")
        }
    )
    candidates = sum(event.get("event") == "candidate_created" for event in events)
    rejected = sum(event.get("event") == "candidate_rejected" for event in events)
    index_updates = sum(event.get("event") == "index_rebuilt" for event in events)
    cat_changes = [event for event in events if event.get("event") in {"memory_used", "memory_reset"}]
    budgets = [event for event in events if event.get("event") == "budget_recorded"]
    budget = budgets[-1].get("budget") if budgets else None
    state = load_state(root, project)
    return {
        "task_id": task_id,
        "project": project,
        "locked": int(locked),
        "candidates": max(0, int(candidates - rejected)),
        "rejected": int(rejected),
        "index_updated": bool(index_updates or locked),
        "cat_changes": len(cat_changes),
        "budget": budget,
        "unconsolidated_tokens": int(state.get("unconsolidated_tokens", 0)),
        "maintenance_due": state.get("maintenance_due", "no"),
    }


def receipt_text(receipt: dict[str, Any]) -> str:
    if receipt["locked"]:
        memory_part = f"已锁定 {receipt['locked']} 条"
    elif receipt["candidates"]:
        memory_part = f"候选 {receipt['candidates']} 条"
    else:
        memory_part = "未产生"
    budget = receipt.get("budget")
    if budget and budget.get("ratio") is not None:
        budget_part = (
            f"{budget['pipeline_tokens']}/{budget['task_total_tokens']}="
            f"{budget['ratio']:.1%} ({budget['status']})"
        )
    else:
        budget_part = "未测量"
    return (
        f"记忆备份：{memory_part}；索引：{'已更新' if receipt['index_updated'] else '无需更新'}；"
        f"CAT：{'变化 ' + str(receipt['cat_changes']) + ' 条' if receipt['cat_changes'] else '无变化'}；"
        f"预算：{budget_part}；128K：{receipt['unconsolidated_tokens']}/131072 "
        f"({receipt['maintenance_due']})。"
    )


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
