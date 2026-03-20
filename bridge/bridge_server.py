from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from aiohttp import web


BOARD_STATUS_ORDER = ("BLOCKED", "IN_PROGRESS", "TODO", "DONE")
BOARD_STATUS_TITLES = {
    "BLOCKED": "Blocked",
    "IN_PROGRESS": "In Progress",
    "TODO": "Todo",
    "DONE": "Done",
}

UPLOAD_ROOT_NAME = ".codex-mobile-uploads"
DEFAULT_ATTACHMENT_PROMPT = "Please inspect the uploaded files from CodeX Mobile and continue this desktop thread."
DEFAULT_ALLOWED_SOURCE_KINDS = "vscode,app,exec,subagent"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def json_response(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=lambda value: json.dumps(value, ensure_ascii=False))


def maybe_parse_json(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def stable_message_id(thread_id: str, line_number: int) -> str:
    return f"{thread_id}:{line_number}"


def unix_to_iso(value: int | float | None) -> str:
    if value is None:
        return utc_now()
    timestamp = float(value)
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso_datetime(value: str | None) -> datetime | None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_board_log_timestamp(value: str | None) -> datetime | None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def role_event_type(role: str) -> str:
    if role == "user":
        return "user.message.created"
    if role == "assistant":
        return "assistant.message.completed"
    return "system.message.created"


def extract_response_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("text"), str):
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"output_text", "input_text", "text"}:
            text = str(item["text"]).strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def compact_text(value: str, line_limit: int = 6, char_limit: int = 320) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    lines = [line.rstrip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""
    clipped = lines[:line_limit]
    text = "\n".join(clipped)
    if len(lines) > line_limit or len(cleaned) > char_limit:
        text = text[:char_limit].rstrip()
        if not text.endswith("..."):
            text = f"{text}..."
    return text


def summarize_tool_call(name: str, arguments: Any) -> str:
    if name == "apply_patch":
        return "Applied patch to workspace files"

    parsed: dict[str, Any] | None = None
    if isinstance(arguments, str):
        parsed = maybe_parse_json(arguments)
    elif isinstance(arguments, dict):
        parsed = arguments

    if parsed:
        if name == "exec_command":
            command = compact_text(str(parsed.get("cmd") or ""))
            workdir = str(parsed.get("workdir") or "")
            if command and workdir:
                return f"{command}\n{workdir}"
            return command or name
        if name == "send_input":
            agent_id = str(parsed.get("id") or "").strip()
            message = compact_text(str(parsed.get("message") or ""))
            if agent_id and message:
                return f"send_input\n{agent_id}\n{message}"
            return compact_text(f"send_input\n{agent_id}" if agent_id else name)
        if name == "wait_agent":
            ids = parsed.get("ids")
            if isinstance(ids, list):
                joined = "\n".join(str(item) for item in ids[:4] if str(item).strip())
                if joined:
                    return f"wait_agent\n{joined}"
        if name == "spawn_agent":
            agent_type = str(parsed.get("agent_type") or "").strip()
            message = compact_text(str(parsed.get("message") or ""))
            if agent_type and message:
                return f"spawn_agent\n{agent_type}\n{message}"
            return compact_text(f"spawn_agent\n{agent_type}" if agent_type else name)
        if name == "close_agent":
            agent_id = str(parsed.get("id") or "").strip()
            return compact_text(f"close_agent\n{agent_id}" if agent_id else name)
        if name.startswith("mcp__playwright__"):
            action = name.removeprefix("mcp__playwright__").replace("_", " ")
            element = str(parsed.get("element") or "")
            return compact_text(f"{action}\n{element}" if element else action)

    return compact_text(name)


def compact_json(value: Any) -> str:
    try:
        return compact_text(json.dumps(value, ensure_ascii=False))
    except TypeError:
        return compact_text(str(value))


@dataclass(frozen=True)
class SourceMetadata:
    raw_text: str
    kind: str
    parent_thread_id: str | None = None
    depth: int | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None


def parse_source_metadata(value: Any) -> SourceMetadata:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                return parse_source_metadata(json.loads(cleaned))
            except json.JSONDecodeError:
                pass
        return SourceMetadata(raw_text=cleaned, kind=cleaned.lower() or "unknown")

    if isinstance(value, dict):
        raw_text = json.dumps(value, ensure_ascii=False)
        subagent = value.get("subagent")
        if isinstance(subagent, dict):
            thread_spawn = subagent.get("thread_spawn")
            if isinstance(thread_spawn, dict):
                depth_raw = thread_spawn.get("depth")
                return SourceMetadata(
                    raw_text=raw_text,
                    kind="subagent",
                    parent_thread_id=str(thread_spawn["parent_thread_id"]) if thread_spawn.get("parent_thread_id") else None,
                    depth=int(depth_raw) if isinstance(depth_raw, (int, float)) else None,
                    agent_nickname=str(thread_spawn["agent_nickname"]) if thread_spawn.get("agent_nickname") else None,
                    agent_role=str(thread_spawn["agent_role"]) if thread_spawn.get("agent_role") else None,
                )
        return SourceMetadata(raw_text=raw_text, kind="unknown")

    if value is None:
        return SourceMetadata(raw_text="", kind="unknown")
    return SourceMetadata(raw_text=str(value), kind=str(value).strip().lower() or "unknown")


def decorate_thread_title(base_title: str, source_meta: SourceMetadata) -> str:
    title = base_title.strip() or "Untitled Thread"
    if source_meta.kind != "subagent":
        return title
    nickname = source_meta.agent_nickname or "Subagent"
    if source_meta.agent_role:
        return f"{nickname} ({source_meta.agent_role})"
    return nickname


def summarize_tool_output(name: str | None, output: Any) -> str:
    tool_name = (name or "tool").strip() or "tool"
    text = str(output or "").strip()
    if not text:
        return ""

    if tool_name in {"exec_command", "write_stdin"}:
        lines = text.splitlines()
        command = ""
        detail_lines: list[str] = []
        if lines and lines[0].startswith("Command: "):
            command = lines[0].removeprefix("Command: ").strip()
        capture_output = False
        for line in lines[1:]:
            if line == "Output:":
                capture_output = True
                continue
            if capture_output:
                detail_lines.append(line)
            elif "Process running with session ID" in line or "Process exited with code" in line:
                detail_lines.append(line.strip())
        detail = compact_text("\n".join(line for line in detail_lines if line.strip()), line_limit=8, char_limit=1000)
        if command and detail:
            return f"{command}\n{detail}"
        if command:
            return command
        return compact_text(text, line_limit=8, char_limit=1000)

    parsed = maybe_parse_json(text)
    if parsed:
        if tool_name == "spawn_agent" and isinstance(parsed.get("agent_id"), str):
            nickname = str(parsed.get("nickname") or "").strip()
            if nickname:
                return f"spawned {nickname}\n{parsed['agent_id']}"
            return f"spawned agent\n{parsed['agent_id']}"
        if tool_name == "wait_agent":
            if parsed.get("timed_out") is True:
                return "wait timed out"
            status = parsed.get("status")
            return compact_json(status if status is not None else parsed)
        if tool_name == "close_agent":
            previous = parsed.get("previous_status")
            return compact_json(previous if previous is not None else parsed)
        return compact_json(parsed)

    return compact_text(text, line_limit=8, char_limit=1000)


def summarize_web_search_call(item: dict[str, Any]) -> str:
    action = item.get("action")
    if not isinstance(action, dict):
        status = str(item.get("status") or "").strip()
        return compact_text(f"web_search\n{status}" if status else "web_search")

    action_type = str(action.get("type") or "").strip()
    if action_type == "search":
        query = str(action.get("query") or "").strip()
        return compact_text(f"search\n{query}" if query else "search")
    if action_type == "open_page":
        url = str(action.get("url") or "").strip()
        return compact_text(f"open_page\n{url}" if url else "open_page")
    return compact_json(action)


def is_desktop_reply_source(source_kind: str) -> bool:
    return source_kind in {"vscode", "app", "bridge"}


def sanitize_filename(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return cleaned or fallback


def normalize_access_mode(value: str | None) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned in {"read-only", "readonly", "read_only"}:
        return "read-only"
    if cleaned in {"danger-full-access", "danger", "full-access", "full_access"}:
        return "danger-full-access"
    return "workspace-write"


def is_image_attachment(filename: str, content_type: str | None) -> bool:
    if (content_type or "").lower().startswith("image/"):
        return True
    guessed, _ = mimetypes.guess_type(filename)
    return bool(guessed and guessed.startswith("image/"))


def save_mobile_uploads(fields: list[Any], cwd: Path, session_id: str) -> list[dict[str, Any]]:
    upload_dir = cwd / UPLOAD_ROOT_NAME / session_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict[str, Any]] = []
    used_names: set[str] = set()

    for index, field in enumerate(fields, start=1):
        original_name = str(getattr(field, "filename", "") or f"attachment-{index}")
        safe_name = sanitize_filename(original_name, f"attachment-{index}")
        stem = Path(safe_name).stem or f"attachment-{index}"
        suffix = Path(safe_name).suffix
        candidate = safe_name
        duplicate_index = 2
        while candidate in used_names or (upload_dir / candidate).exists():
            candidate = f"{stem}-{duplicate_index}{suffix}"
            duplicate_index += 1

        payload = getattr(field, "file").read()
        destination = upload_dir / candidate
        destination.write_bytes(payload)
        used_names.add(candidate)

        try:
            relative_path = destination.relative_to(cwd).as_posix()
        except ValueError:
            relative_path = str(destination)

        content_type = str(getattr(field, "content_type", "") or "")
        saved.append(
            {
                "filename": candidate,
                "original_name": original_name,
                "path": destination,
                "relative_path": relative_path,
                "content_type": content_type,
                "is_image": is_image_attachment(candidate, content_type),
            }
        )

    return saved


def build_mobile_prompt(text: str, attachments: list[dict[str, Any]]) -> str:
    body = text.strip() or DEFAULT_ATTACHMENT_PROMPT
    if not attachments:
        return body

    lines = [
        "Files uploaded from CodeX Mobile are now available inside the current workspace:",
    ]
    for item in attachments:
        kind = "image" if item["is_image"] else "file"
        lines.append(f"- {item['original_name']} ({kind}): {item['relative_path']}")
    lines.extend(["", "User message:", body])
    return "\n".join(lines)


@lru_cache(maxsize=512)
def infer_project_identity(cwd: str | None) -> tuple[str | None, str | None]:
    cleaned = (cwd or "").strip()
    if not cleaned:
        return (None, None)

    path = Path(cleaned).expanduser()
    resolved = path.resolve() if path.exists() else path

    candidates = [resolved]
    candidates.extend(resolved.parents)
    for candidate in candidates:
        git_marker = candidate / ".git"
        if git_marker.exists():
            return (str(candidate), candidate.name or str(candidate))

    name = resolved.name or cleaned
    return (str(resolved), name)


@dataclass
class ChatMessage:
    id: str
    role: str
    text: str
    created_at: str
    state: str = "done"
    kind: str = "message"
    phase: str | None = None
    tool_name: str | None = None


@dataclass
class SessionEvent:
    seq: int
    type: str
    timestamp: str
    payload: dict[str, Any]


@dataclass
class ThreadRecord:
    thread_id: str
    title: str
    source: str
    source_kind: str
    git_branch: str | None
    cwd: str
    created_at: str
    updated_at: str
    rollout_path: str
    model_provider: str | None
    cli_version: str | None
    first_user_message: str | None
    parent_thread_id: str | None
    source_depth: int | None
    agent_nickname: str | None
    agent_role: str | None


@dataclass
class ParsedFileDelta:
    messages: list[ChatMessage]
    created_at: str | None
    updated_at: str | None
    cwd: str | None
    source: str | None
    source_kind: str | None
    originator: str | None
    model_provider: str | None
    cli_version: str | None
    parent_thread_id: str | None
    source_depth: int | None
    agent_nickname: str | None
    agent_role: str | None
    running: bool
    next_offset: int
    next_line_number: int
    pending_fragment: str
    stat_size: int
    stat_mtime_ns: int
    reset_applied: bool
    tool_call_names: dict[str, str]


@dataclass
class BoardTaskRecord:
    id: str
    thread: str
    title: str
    owner: str
    status: str
    depends_on: str
    output: str
    line_no: int


@dataclass
class ChatSession:
    id: str
    title: str
    created_at: str
    updated_at: str
    thread_id: str | None = None
    cwd: str | None = None
    project_root: str | None = None
    project_name: str | None = None
    source: str = "bridge"
    source_kind: str = "bridge"
    git_branch: str | None = None
    originator: str | None = None
    imported: bool = False
    desktop_thread: bool = False
    data_source: str = "bridge"
    rollout_path: str | None = None
    model_provider: str | None = None
    cli_version: str | None = None
    first_user_message: str | None = None
    parent_thread_id: str | None = None
    source_depth: int | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
    bridge_reply_available: bool = True
    messages: list[ChatMessage] = field(default_factory=list)
    events: list[SessionEvent] = field(default_factory=list)
    subscribers: set[asyncio.Queue[SessionEvent]] = field(default_factory=set)
    tool_call_names: dict[str, str] = field(default_factory=dict)
    next_seq: int = 1
    running: bool = False
    active_task: asyncio.Task[None] | None = None
    last_disk_mtime_ns: int = 0
    last_disk_size: int = 0
    last_read_offset: int = 0
    last_line_number: int = 0
    pending_fragment: str = ""

    def summary(self) -> dict[str, Any]:
        preview_message = next((message for message in reversed(self.messages) if message.role != "tool"), None)
        if preview_message is None and self.messages:
            preview_message = self.messages[-1]
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "thread_id": self.thread_id,
            "cwd": self.cwd,
            "project_root": self.project_root,
            "project_name": self.project_name,
            "source": self.source,
            "source_kind": self.source_kind,
            "git_branch": self.git_branch,
            "originator": self.originator,
            "imported": self.imported,
            "desktop_thread": self.desktop_thread,
            "data_source": self.data_source,
            "rollout_path": self.rollout_path,
            "model_provider": self.model_provider,
            "cli_version": self.cli_version,
            "first_user_message": self.first_user_message,
            "parent_thread_id": self.parent_thread_id,
            "source_depth": self.source_depth,
            "agent_nickname": self.agent_nickname,
            "agent_role": self.agent_role,
            "bridge_reply_available": self.bridge_reply_available,
            "running": self.running,
            "message_count": len(self.messages),
            "last_message_preview": preview_message.text[:180] if preview_message else "",
        }

    def detail(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "messages": [asdict(message) for message in self.messages],
            "last_event_sequence": self.events[-1].seq if self.events else 0,
        }


def read_threads_from_sqlite(db_path: Path, limit: int, allowed_sources: set[str]) -> list[ThreadRecord]:
    uri = f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT id, title, source, cwd, created_at, updated_at, rollout_path, model_provider, cli_version, first_user_message, agent_nickname, agent_role, git_branch
            FROM threads
            WHERE archived = 0
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit * 3,),
        )
        records: list[ThreadRecord] = []
        seen_ids: set[str] = set()
        for row in cursor.fetchall():
            source_meta = parse_source_metadata(row["source"])
            if allowed_sources and source_meta.kind not in allowed_sources and source_meta.raw_text not in allowed_sources:
                continue
            thread_id = str(row["id"])
            if thread_id in seen_ids:
                continue
            seen_ids.add(thread_id)
            base_title = str(row["title"] or row["first_user_message"] or f"Thread {thread_id[:8]}")
            records.append(
                ThreadRecord(
                    thread_id=thread_id,
                    title=decorate_thread_title(base_title, source_meta),
                    source=source_meta.raw_text,
                    source_kind=source_meta.kind,
                    git_branch=str(row["git_branch"]) if row["git_branch"] else None,
                    cwd=str(row["cwd"] or ""),
                    created_at=unix_to_iso(row["created_at"]),
                    updated_at=unix_to_iso(row["updated_at"]),
                    rollout_path=str(row["rollout_path"] or ""),
                    model_provider=str(row["model_provider"]) if row["model_provider"] else None,
                    cli_version=str(row["cli_version"]) if row["cli_version"] else None,
                    first_user_message=str(row["first_user_message"]) if row["first_user_message"] else None,
                    parent_thread_id=source_meta.parent_thread_id,
                    source_depth=source_meta.depth,
                    agent_nickname=str(row["agent_nickname"]) if row["agent_nickname"] else source_meta.agent_nickname,
                    agent_role=str(row["agent_role"]) if row["agent_role"] else source_meta.agent_role,
                )
            )
            if len(records) >= limit:
                break
        return records
    finally:
        connection.close()


def read_recent_projects_from_sqlite(db_path: Path, limit: int) -> list[dict[str, str]]:
    uri = f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT cwd, updated_at
            FROM threads
            WHERE cwd IS NOT NULL AND cwd != ''
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit * 20,),
        )
        projects: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in cursor.fetchall():
            project_root, project_name = infer_project_identity(str(row["cwd"] or ""))
            if not project_root or project_root in seen:
                continue
            seen.add(project_root)
            projects.append(
                {
                    "id": project_root,
                    "name": project_name or Path(project_root).name or project_root,
                    "path": project_root,
                    "updated_at": unix_to_iso(row["updated_at"]),
                }
            )
            if len(projects) >= limit:
                break
        return projects
    finally:
        connection.close()


def read_board_runtime_sessions_from_sqlite(
    db_path: Path,
    limit: int,
    allowed_sources: set[str],
    board_root: str,
    target_repo_root: str | None,
    worktree_root: str | None = None,
) -> list[dict[str, Any]]:
    uri = f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.cursor()
        board_path = Path(board_root).expanduser()
        targets: list[str] = []
        for path in board_scope_paths(board_path, target_repo_root, worktree_root):
            for alias in sorted(path_alias_strings(path)):
                if alias not in targets:
                    targets.append(alias)

        where_clauses = ["(cwd = ? OR cwd LIKE ?)"] * len(targets)
        parameters: list[Any] = []
        for target in targets:
            parameters.extend([target, f"{target}/%"])
        cursor.execute(
            f"""
            SELECT id, title, source, cwd, updated_at, git_branch, agent_nickname, agent_role, first_user_message
            FROM threads
            WHERE archived = 0
              AND cwd IS NOT NULL
              AND cwd != ''
              AND ({' OR '.join(where_clauses)})
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (*parameters, limit),
        )

        sessions: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for row in cursor.fetchall():
            thread_id = str(row["id"] or "")
            if not thread_id or thread_id in seen_ids:
                continue
            source_meta = parse_source_metadata(row["source"])
            if allowed_sources and source_meta.kind not in allowed_sources and source_meta.raw_text not in allowed_sources:
                continue

            cwd = str(row["cwd"] or "")
            base_title = str(row["title"] or f"Thread {thread_id[:8]}")
            sessions.append(
                {
                    "id": thread_id,
                    "title": decorate_thread_title(base_title, source_meta),
                    "source": source_meta.raw_text,
                    "source_kind": source_meta.kind,
                    "git_branch": str(row["git_branch"]) if row["git_branch"] else None,
                    "cwd": cwd,
                    "updated_at": unix_to_iso(row["updated_at"]),
                    "parent_thread_id": source_meta.parent_thread_id,
                    "agent_nickname": str(row["agent_nickname"]) if row["agent_nickname"] else source_meta.agent_nickname,
                    "agent_role": str(row["agent_role"]) if row["agent_role"] else source_meta.agent_role,
                    "first_user_message": str(row["first_user_message"] or ""),
                    "running": False,
                    "last_message_preview": "",
                }
            )
            seen_ids.add(thread_id)
        return sessions
    finally:
        connection.close()


def is_board_root(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "TASK_BOARD.md").exists()
        and (path / "THREADS.json").exists()
        and (path / "COMM_LOG.md").exists()
    )


def board_id_for_path(board_root: Path) -> str:
    encoded = base64.urlsafe_b64encode(str(board_root).encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def discover_boards_in_folder(folder: Path) -> list[Path]:
    resolved = folder.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        return []

    boards: list[Path] = []
    seen: set[Path] = set()

    if is_board_root(resolved):
        seen.add(resolved)
        boards.append(resolved)

    for child in sorted(resolved.iterdir(), key=lambda item: item.name.lower()):
        if child.name.startswith("."):
            continue
        child_resolved = child.resolve()
        if child_resolved in seen or not is_board_root(child_resolved):
            continue
        seen.add(child_resolved)
        boards.append(child_resolved)

    return boards


def discover_board_folders(
    workspace: Path,
    configured_folders: tuple[Path, ...],
    configured_board_roots: tuple[Path, ...],
    recent_project_paths: list[str],
) -> list[Path]:
    candidates: list[Path] = [workspace, workspace.parent]
    candidates.extend(configured_folders)
    for board_root in configured_board_roots:
        candidates.append(board_root)
        candidates.append(board_root.parent)
    for raw_path in recent_project_paths:
        candidate = Path(raw_path).expanduser()
        candidates.append(candidate)
        candidates.append(candidate.parent)

    folders: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not str(candidate).strip():
            continue
        resolved = candidate.resolve() if candidate.exists() else candidate
        if resolved in seen or not resolved.exists() or not resolved.is_dir():
            continue
        if not discover_boards_in_folder(resolved):
            continue
        seen.add(resolved)
        folders.append(resolved)

    return folders


def discover_board_roots(workspace: Path, configured_roots: tuple[Path, ...]) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    for root in configured_roots:
        resolved = root.expanduser().resolve()
        if resolved in seen or not is_board_root(resolved):
            continue
        seen.add(resolved)
        candidates.append(resolved)

    for child in sorted(workspace.iterdir(), key=lambda item: item.name.lower()):
        if child.name.startswith(".") or child.resolve() in seen:
            continue
        if not is_board_root(child):
            continue
        resolved = child.resolve()
        seen.add(resolved)
        candidates.append(resolved)

    return candidates


def parse_board_title(board_root: Path) -> str:
    readme_path = board_root / "README.md"
    if readme_path.exists():
        for line in readme_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                if title:
                    return title
    return board_root.name.replace("-", " ").replace("_", " ").title()


def load_board_threads(threads_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(threads_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"invalid THREADS.json: {threads_path}")
    return [row for row in payload if isinstance(row, dict)]


def parse_board_tasks(path: Path) -> list[BoardTaskRecord]:
    tasks: list[BoardTaskRecord] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.startswith("|") or "|---" in line:
            continue
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) != 7 or cells[0] == "ID":
            continue
        tasks.append(
            BoardTaskRecord(
                id=cells[0],
                thread=cells[1],
                title=cells[2],
                owner=cells[3],
                status=cells[4],
                depends_on=cells[5],
                output=cells[6],
                line_no=idx,
            )
        )
    return tasks


def parse_board_comm_log(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    latest: dict[str, dict[str, Any]] = {}
    kickoff_latest: dict[str, dict[str, Any]] = {}
    last_invocation: dict[str, dict[str, Any]] = {}
    active_kickoff: dict[str, tuple[dict[str, Any], datetime]] = {}
    in_code_block = False

    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not line.startswith("["):
            continue

        try:
            first = line.index("] [")
            ts = line[1:first]
            rest = line[first + 3 :]
            second = rest.index("] [type: ")
            thread = rest[:second]
            rest2 = rest[second + len("] [type: ") :]
            third = rest2.index("] ")
            kind = rest2[:third]
            message = rest2[third + 2 :]
        except ValueError:
            continue

        try:
            parsed_ts = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        except ValueError:
            parsed_ts = None

        latest[thread] = {
            "timestamp": ts,
            "type": kind,
            "message": message,
            "line_no": idx,
        }

        if kind == "kickoff":
            kickoff = {
                "timestamp": ts,
                "message": message,
                "line_no": idx,
            }
            kickoff_latest[thread] = kickoff
            if parsed_ts is not None:
                active_kickoff[thread] = (kickoff, parsed_ts)
            else:
                active_kickoff.pop(thread, None)
            continue

        kickoff_row, start_ts = active_kickoff.get(thread, (None, None))
        if kickoff_row is None or start_ts is None or parsed_ts is None or parsed_ts < start_ts:
            continue

        last_invocation[thread] = {
            "start_timestamp": kickoff_row["timestamp"],
            "end_timestamp": ts,
            "elapsed_seconds": max(int((parsed_ts - start_ts).total_seconds()), 0),
            "end_type": kind,
            "start_line_no": kickoff_row["line_no"],
            "end_line_no": idx,
        }

    return {
        "latest": latest,
        "kickoff_latest": kickoff_latest,
        "last_invocation": last_invocation,
    }


def read_git(repo: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def read_board_config(board_root: Path) -> dict[str, Any] | None:
    config_path = board_root / "coordination.config.json"
    if not config_path.exists():
        return None
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


def branch_matches_thread(branch_name: str, thread_id: str, persistent_branch: str | None) -> bool:
    cleaned = branch_name.strip()
    if not cleaned:
        return False
    if persistent_branch and cleaned == persistent_branch:
        return True
    return cleaned.startswith(f"codex/{thread_id}-")


def board_scope_paths(
    board_root: Path,
    target_repo_root: str | None,
    worktree_root: str | None = None,
) -> list[Path]:
    targets: list[Path] = []
    extra_roots = [
        str(board_root),
        target_repo_root,
        str(Path(target_repo_root).expanduser() / ".codex-worktrees") if target_repo_root else None,
        worktree_root,
        str(board_root.parent / ".codex-worktrees")
        if os.environ.get("CODEX_BRIDGE_INCLUDE_LEGACY_WORKTREE_ROOTS", "1") != "0"
        else None,
        str(board_root.parent)
        if os.environ.get("CODEX_BRIDGE_INCLUDE_BOARD_PARENT_ROOT", "0") == "1"
        else None,
    ]

    seen: set[Path] = set()
    for raw in extra_roots:
        for alias in path_alias_strings(str(raw or "")):
            path = Path(alias)
            if path in seen:
                continue
            seen.add(path)
            targets.append(path)
    return targets


def path_alias_strings(raw_path: str | Path) -> set[str]:
    path = raw_path if isinstance(raw_path, Path) else Path(str(raw_path)).expanduser()
    candidates = {str(path)}
    with contextlib.suppress(Exception):
        candidates.add(str(path.resolve()))

    aliases: set[str] = set()
    for candidate in candidates:
        cleaned = candidate.strip()
        if not cleaned:
            continue
        aliases.add(cleaned)
        if cleaned.startswith("/private/"):
            aliases.add(cleaned.removeprefix("/private"))
        elif cleaned.startswith("/var/") or cleaned.startswith("/tmp/"):
            aliases.add(f"/private{cleaned}")
    return {item for item in aliases if item}


def path_belongs_to_board_scope(
    session_cwd: str,
    board_root: Path,
    target_repo_root: str | None,
    worktree_root: str | None = None,
) -> bool:
    cleaned = session_cwd.strip()
    if not cleaned:
        return False
    path = Path(cleaned).expanduser()
    resolved = path.resolve() if path.exists() else path

    for target in board_scope_paths(board_root, target_repo_root, worktree_root):
        if resolved == target:
            return True
        with contextlib.suppress(ValueError):
            resolved.relative_to(target)
            return True
    return False


def normalize_identity_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def build_session_match_text(session: dict[str, Any]) -> tuple[str, str]:
    parts = [
        session.get("title"),
        session.get("first_user_message"),
        session.get("cwd"),
        session.get("source"),
        session.get("_match_text"),
    ]
    raw_text = "\n".join(str(part).lower() for part in parts if str(part or "").strip())
    return raw_text, normalize_identity_text(raw_text)


def match_thread_identity(session: dict[str, Any], thread_def: dict[str, Any]) -> int:
    raw_text, normalized_text = build_session_match_text(session)
    if not raw_text and not normalized_text:
        return 0

    best = 0
    thread_id = str(thread_def.get("id") or "").strip().lower()
    if thread_id:
        if re.search(rf"\byou are {re.escape(thread_id)}\b", raw_text):
            best = max(best, 900)
        if re.search(rf"\bact as [^\n]*\b{re.escape(thread_id)}\b", raw_text):
            best = max(best, 850)
        if re.search(rf"(?<!codex/)\b{re.escape(thread_id)}\b(?!-)", raw_text):
            best = max(best, 500)

    name = str(thread_def.get("name") or "").strip()
    if name:
        lowered_name = name.lower()
        normalized_name = normalize_identity_text(name)
        if lowered_name in raw_text or (normalized_name and normalized_name in normalized_text):
            best = max(best, 350)

    role = str(thread_def.get("role") or "").strip()
    if role:
        lowered_role = role.lower()
        normalized_role = normalize_identity_text(role)
        if len(lowered_role) >= 8 and lowered_role in raw_text:
            best = max(best, 180)
        if len(normalized_role) >= 8 and normalized_role in normalized_text:
            best = max(best, 170)

    return best


def build_board_runtime_index(
    board_root: Path,
    target_repo_root: str | None,
    worktree_root: str | None,
    thread_defs: list[dict[str, Any]],
    session_summaries: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    config = read_board_config(board_root) or {}
    persistent_raw = config.get("persistent_branches")
    persistent_branches = {
        str(thread_id): str(branch_name)
        for thread_id, branch_name in persistent_raw.items()
        if isinstance(thread_id, str) and isinstance(branch_name, str)
    } if isinstance(persistent_raw, dict) else {}

    thread_ids = [str(row.get("id") or "") for row in thread_defs if str(row.get("id") or "")]
    thread_index = {
        str(row.get("id") or ""): row
        for row in thread_defs
        if str(row.get("id") or "")
    }
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {thread_id: {} for thread_id in thread_ids}

    for session in session_summaries:
        cwd = str(session.get("cwd") or "")
        if not path_belongs_to_board_scope(cwd, board_root, target_repo_root, worktree_root):
            continue

        identity_matches = [
            (match_thread_identity(session, thread_index[thread_id]), thread_id)
            for thread_id in thread_ids
        ]
        identity_matches = [(score, thread_id) for score, thread_id in identity_matches if score > 0]

        matched_thread_id: str | None = None
        if identity_matches:
            identity_matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
            if len(identity_matches) == 1 or identity_matches[0][0] > identity_matches[1][0]:
                matched_thread_id = identity_matches[0][1]

        if matched_thread_id is None:
            branch_name = str(session.get("git_branch") or "")
            matched_thread_id = next(
                (
                    thread_id
                    for thread_id in thread_ids
                    if branch_matches_thread(branch_name, thread_id, persistent_branches.get(thread_id))
                ),
                None,
            )
        if matched_thread_id is None:
            continue

        group_id = str(session.get("parent_thread_id") or session.get("id") or "")
        if not group_id:
            continue
        grouped[matched_thread_id].setdefault(group_id, []).append(session)

    runtime: dict[str, dict[str, Any]] = {}
    for thread_id, groups in grouped.items():
        if not groups:
            continue

        selected_group = max(
            groups.values(),
            key=lambda items: max(str(item.get("updated_at") or "") for item in items),
        )
        selected_group = sorted(selected_group, key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        latest = selected_group[0]
        runtime[thread_id] = {
            "session_count": len(selected_group),
            "subagent_count": sum(1 for item in selected_group if item.get("source_kind") == "subagent"),
            "running": any(bool(item.get("running")) for item in selected_group),
            "updated_at": latest.get("updated_at"),
            "git_branch": latest.get("git_branch"),
            "latest_title": latest.get("title"),
            "last_message_preview": latest.get("last_message_preview"),
            "source_kind": latest.get("source_kind"),
            "agent_nickname": latest.get("agent_nickname"),
            "agent_role": latest.get("agent_role"),
        }

    return runtime


def build_board_repo_snapshot(
    board_root: Path,
    thread_defs: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], str | None, str | None, str | None]:
    raw = read_board_config(board_root)
    if raw is None:
        return None, {}, None, None, None

    target_repo_raw = raw.get("target_repo")
    worktree_root_raw = raw.get("worktree_root")
    base_branch = str(raw.get("base_branch") or "") or None
    target_repo = Path(str(target_repo_raw)).expanduser().resolve() if target_repo_raw else None
    worktree_root = (
        str(Path(str(worktree_root_raw)).expanduser().resolve())
        if worktree_root_raw
        else None
    )
    if target_repo is None:
        return {"configured": False, "error": "missing target_repo"}, {}, base_branch, None, worktree_root
    if not target_repo.exists():
        return {
            "configured": False,
            "error": f"target repo not found: {target_repo}",
        }, {}, base_branch, str(target_repo), worktree_root

    local_branches = [
        line
        for line in (read_git(target_repo, "for-each-ref", "refs/heads", "--format=%(refname:short)") or "").splitlines()
        if line
    ]
    remote_branches = [
        line
        for line in (read_git(target_repo, "for-each-ref", "refs/remotes/origin", "--format=%(refname:short)") or "").splitlines()
        if line
    ]
    worktree_rows = (read_git(target_repo, "worktree", "list", "--porcelain") or "").splitlines()
    worktrees: dict[str, str] = {}
    current_path: str | None = None
    for line in worktree_rows:
        if line.startswith("worktree "):
            current_path = line.split(" ", 1)[1]
        elif line.startswith("branch ") and current_path:
            worktrees[line.split(" ", 1)[1].replace("refs/heads/", "")] = current_path
            current_path = None

    branch_snapshots: dict[str, dict[str, Any]] = {}
    for row in thread_defs:
        thread_id = str(row.get("id") or "")
        if not thread_id:
            continue
        prefix = f"codex/{thread_id}-"
        local_matches = [branch for branch in local_branches if branch.startswith(prefix)]
        remote_matches = [branch for branch in remote_branches if branch.split("/")[-1].startswith(f"{thread_id}-")]
        branch_snapshots[thread_id] = {
            "expected_prefix": prefix,
            "local": [{"name": branch, "worktree": worktrees.get(branch)} for branch in local_matches],
            "remote": remote_matches,
        }

    valid_threads = {str(row.get("id") or "") for row in thread_defs}
    legacy_local: list[str] = []
    for branch in local_branches:
        if base_branch and branch == base_branch:
            continue
        if not branch.startswith("codex/thread"):
            legacy_local.append(branch)
            continue
        tail = branch[len("codex/thread") :]
        if "-" not in tail:
            legacy_local.append(branch)
            continue
        idx, _scope = tail.split("-", 1)
        if not idx.isdigit() or f"thread{idx}" not in valid_threads:
            legacy_local.append(branch)

    return (
        {
            "configured": True,
            "current_branch": read_git(target_repo, "branch", "--show-current") or "",
            "dirty": bool(read_git(target_repo, "status", "--porcelain")),
            "legacy_local_branches": legacy_local,
        },
        branch_snapshots,
        base_branch,
        str(target_repo),
        worktree_root,
    )


def select_board_task(thread_id: str, tasks: list[BoardTaskRecord]) -> BoardTaskRecord | None:
    thread_tasks = [task for task in tasks if task.thread == thread_id]
    for status in BOARD_STATUS_ORDER:
        matches = [task for task in thread_tasks if task.status == status]
        if matches:
            return matches[-1]
    return None


def serialize_board_task(
    task: BoardTaskRecord,
    thread_meta: dict[str, Any] | None,
    latest_log: dict[str, Any] | None,
    branches: dict[str, Any] | None,
    runtime: dict[str, Any] | None,
) -> dict[str, Any]:
    meta = thread_meta or {}
    return {
        "id": task.id,
        "thread": task.thread,
        "title": task.title,
        "owner": task.owner,
        "status": task.status,
        "depends_on": task.depends_on,
        "output": task.output,
        "line_no": task.line_no,
        "slot": str(meta.get("slot") or ""),
        "display_name": str(meta.get("name") or task.thread),
        "role": str(meta.get("role") or ""),
        "auto_branch": bool(meta.get("auto_branch", False)),
        "latest_log": latest_log,
        "branches": branches,
        "runtime": runtime,
    }


def annotate_runtime_snapshot(
    runtime: dict[str, Any] | None,
    latest_log: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if runtime is None:
        return None

    annotated = dict(runtime)
    updated_at = parse_iso_datetime(str(runtime.get("updated_at") or ""))
    latest_log_dt = parse_board_log_timestamp(str((latest_log or {}).get("timestamp") or ""))
    stale = False
    stale_reason: str | None = None

    if updated_at and latest_log_dt:
        delta_seconds = (latest_log_dt - updated_at.astimezone(timezone.utc)).total_seconds()
        if delta_seconds > 12 * 3600:
            stale = True
            stale_reason = "log_newer"
    elif updated_at and not bool(runtime.get("running")):
        age = datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)
        if age.total_seconds() > 72 * 3600:
            stale = True
            stale_reason = "aged_out"

    annotated["stale"] = stale
    annotated["stale_reason"] = stale_reason
    return annotated


def read_board_snapshot(board_root: Path, session_summaries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    task_board_path = board_root / "TASK_BOARD.md"
    threads_path = board_root / "THREADS.json"
    comm_log_path = board_root / "COMM_LOG.md"
    thread_defs = load_board_threads(threads_path)
    tasks = parse_board_tasks(task_board_path)
    logs = parse_board_comm_log(comm_log_path)
    title = parse_board_title(board_root)
    repo_snapshot, branch_snapshots, base_branch, target_repo_root, worktree_root = build_board_repo_snapshot(board_root, thread_defs)
    runtime_index = build_board_runtime_index(
        board_root,
        target_repo_root,
        worktree_root,
        thread_defs,
        session_summaries or [],
    )
    thread_index = {str(row.get("id") or ""): row for row in thread_defs}

    totals = {"blocked": 0, "in_progress": 0, "todo": 0, "done": 0}
    for task in tasks:
        if task.status == "BLOCKED":
            totals["blocked"] += 1
        elif task.status == "IN_PROGRESS":
            totals["in_progress"] += 1
        elif task.status == "DONE":
            totals["done"] += 1
        else:
            totals["todo"] += 1

    columns: list[dict[str, Any]] = []
    for status in BOARD_STATUS_ORDER:
        column_tasks = [
            serialize_board_task(
                task,
                thread_index.get(task.thread),
                logs["latest"].get(task.thread),
                branch_snapshots.get(task.thread),
                annotate_runtime_snapshot(
                    runtime_index.get(task.thread),
                    logs["latest"].get(task.thread),
                ),
            )
            for task in tasks
            if task.status == status
        ]
        columns.append(
            {
                "id": status.lower(),
                "status": status,
                "title": BOARD_STATUS_TITLES[status],
                "count": len(column_tasks),
                "tasks": column_tasks,
            }
        )

    threads: list[dict[str, Any]] = []
    for row in sorted(thread_defs, key=lambda item: str(item.get("slot") or item.get("id") or "")):
        thread_id = str(row.get("id") or "")
        selected_task = select_board_task(thread_id, tasks)
        threads.append(
            {
                "thread": thread_id,
                "slot": str(row.get("slot") or ""),
                "display_name": str(row.get("name") or thread_id),
                "role": str(row.get("role") or ""),
                "auto_branch": bool(row.get("auto_branch", False)),
                "task": (
                    serialize_board_task(
                        selected_task,
                        row,
                        logs["latest"].get(thread_id),
                        branch_snapshots.get(thread_id),
                        annotate_runtime_snapshot(
                            runtime_index.get(thread_id),
                            logs["latest"].get(thread_id),
                        ),
                    )
                    if selected_task is not None
                    else None
                ),
                "last_log": logs["latest"].get(thread_id),
                "runtime_start": logs["kickoff_latest"].get(thread_id),
                "last_invocation": logs["last_invocation"].get(thread_id),
                "branches": branch_snapshots.get(thread_id),
                "runtime": annotate_runtime_snapshot(
                    runtime_index.get(thread_id),
                    logs["latest"].get(thread_id),
                ),
            }
        )

    updated_at = max(
        path.stat().st_mtime
        for path in (task_board_path, threads_path, comm_log_path)
        if path.exists()
    )
    return {
        "id": board_id_for_path(board_root),
        "title": title,
        "path": str(board_root),
        "folder_path": str(board_root.parent),
        "generated_at": utc_now(),
        "updated_at": unix_to_iso(updated_at),
        "task_count": len(tasks),
        "thread_count": len(thread_defs),
        "base_branch": base_branch,
        "target_repo_root": target_repo_root,
        "repo": repo_snapshot,
        "totals": totals,
        "columns": columns,
        "threads": threads,
    }


def parse_rollout_delta(
    path: Path,
    thread_id: str,
    start_offset: int,
    start_line_number: int,
    pending_fragment: str,
    start_tool_call_names: dict[str, str],
    initial_running: bool,
    reset: bool,
) -> ParsedFileDelta:
    stat_result = path.stat()
    read_offset = 0 if reset else start_offset
    line_number = 0 if reset else start_line_number
    running = False if reset else initial_running

    with path.open("rb") as handle:
        handle.seek(read_offset)
        raw = handle.read()

    decoded = pending_fragment + raw.decode("utf-8", errors="replace")
    pieces = decoded.splitlines(keepends=True)
    fragment = ""
    if pieces and not pieces[-1].endswith(("\n", "\r")):
        fragment = pieces.pop()

    created_at: str | None = None
    updated_at: str | None = None
    cwd: str | None = None
    source: str | None = None
    source_kind: str | None = None
    originator: str | None = None
    model_provider: str | None = None
    cli_version: str | None = None
    parent_thread_id: str | None = None
    source_depth: int | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
    messages: list[ChatMessage] = []
    tool_call_names = {} if reset else dict(start_tool_call_names)

    for piece in pieces:
        line_number += 1
        payload = maybe_parse_json(piece.rstrip("\r\n"))
        if payload is None:
            continue

        timestamp = payload.get("timestamp")
        if isinstance(timestamp, str):
            updated_at = timestamp

        event_type = payload.get("type")
        if event_type == "session_meta":
            meta = payload.get("payload", {})
            if isinstance(meta, dict):
                if isinstance(meta.get("timestamp"), str):
                    created_at = str(meta["timestamp"])
                if isinstance(meta.get("cwd"), str):
                    cwd = str(meta["cwd"])
                if isinstance(meta.get("source"), str):
                    source = str(meta["source"])
                if isinstance(meta.get("originator"), str):
                    originator = str(meta["originator"])
                source_meta = parse_source_metadata(meta.get("source"))
                if source_meta.raw_text:
                    source = source_meta.raw_text
                source_kind = source_meta.kind
                parent_thread_id = (
                    str(meta["forked_from_id"])
                    if meta.get("forked_from_id")
                    else source_meta.parent_thread_id
                )
                source_depth = source_meta.depth
                if isinstance(meta.get("agent_nickname"), str):
                    agent_nickname = str(meta["agent_nickname"])
                elif source_meta.agent_nickname:
                    agent_nickname = source_meta.agent_nickname
                if isinstance(meta.get("agent_role"), str):
                    agent_role = str(meta["agent_role"])
                elif source_meta.agent_role:
                    agent_role = source_meta.agent_role
                if isinstance(meta.get("model_provider"), str):
                    model_provider = str(meta["model_provider"])
                if isinstance(meta.get("cli_version"), str):
                    cli_version = str(meta["cli_version"])
            continue

        if event_type == "event_msg":
            event = payload.get("payload", {})
            if not isinstance(event, dict):
                continue

            kind = event.get("type")
            if kind == "task_started":
                running = True
                continue
            if kind == "task_complete":
                running = False
                continue
            if kind == "user_message":
                text = str(event.get("message", "")).strip()
                if text:
                    messages.append(
                        ChatMessage(
                            id=stable_message_id(thread_id, line_number),
                            role="user",
                            text=text,
                            created_at=str(timestamp or updated_at or utc_now()),
                        )
                    )
            continue

        if event_type != "response_item":
            continue

        item = payload.get("payload", {})
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type == "message" and item.get("role") == "assistant":
            text = extract_response_text(item.get("content"))
            phase = str(item.get("phase")) if item.get("phase") else None
            if text:
                messages.append(
                    ChatMessage(
                        id=stable_message_id(thread_id, line_number),
                        role="assistant",
                        text=text,
                        created_at=str(timestamp or updated_at or utc_now()),
                        state=phase or "done",
                        kind="commentary" if phase == "commentary" else "message",
                        phase=phase,
                    )
                )
            continue

        if item_type in {"function_call", "custom_tool_call"}:
            name = str(item.get("name") or "tool")
            call_id = str(item.get("call_id") or "")
            if call_id:
                tool_call_names[call_id] = name
            arguments = item.get("input") if item_type == "custom_tool_call" else item.get("arguments")
            text = summarize_tool_call(name, arguments)
            if text:
                messages.append(
                    ChatMessage(
                        id=stable_message_id(thread_id, line_number),
                        role="tool",
                        text=text,
                        created_at=str(timestamp or updated_at or utc_now()),
                        state="done",
                        kind="tool_call",
                        tool_name=name,
                    )
                )
            continue

        if item_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(item.get("call_id") or "")
            name = tool_call_names.get(call_id, "tool")
            text = summarize_tool_output(name, item.get("output"))
            if text:
                messages.append(
                    ChatMessage(
                        id=stable_message_id(thread_id, line_number),
                        role="tool",
                        text=text,
                        created_at=str(timestamp or updated_at or utc_now()),
                        state="done",
                        kind="tool_call",
                        tool_name=name,
                    )
                )
            continue

        if item_type == "web_search_call":
            text = summarize_web_search_call(item)
            if text:
                messages.append(
                    ChatMessage(
                        id=stable_message_id(thread_id, line_number),
                        role="tool",
                        text=text,
                        created_at=str(timestamp or updated_at or utc_now()),
                        state="done",
                        kind="tool_call",
                        tool_name="web_search",
                    )
                )

    return ParsedFileDelta(
        messages=messages,
        created_at=created_at,
        updated_at=updated_at,
        cwd=cwd,
        source=source,
        source_kind=source_kind,
        originator=originator,
        model_provider=model_provider,
        cli_version=cli_version,
        parent_thread_id=parent_thread_id,
        source_depth=source_depth,
        agent_nickname=agent_nickname,
        agent_role=agent_role,
        running=running,
        next_offset=stat_result.st_size,
        next_line_number=line_number,
        pending_fragment=fragment,
        stat_size=stat_result.st_size,
        stat_mtime_ns=stat_result.st_mtime_ns,
        reset_applied=reset,
        tool_call_names=tool_call_names,
    )


class SessionStore:
    def __init__(
        self,
        workspace: Path,
        codex_home: Path,
        threads_db_path: Path,
        scan_limit: int,
        poll_interval: float,
        allowed_sources: set[str],
        board_folders: tuple[Path, ...],
        board_roots: tuple[Path, ...],
    ) -> None:
        self.workspace = workspace
        self.codex_home = codex_home
        self.threads_db_path = threads_db_path
        self.scan_limit = scan_limit
        self.poll_interval = poll_interval
        self.allowed_sources = allowed_sources
        self.board_folders = board_folders
        self.board_roots = board_roots
        self.sessions: dict[str, ChatSession] = {}
        self.lock = asyncio.Lock()
        self.sync_lock = asyncio.Lock()
        self.watch_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.sync_codex_threads()
        self.watch_task = asyncio.create_task(self.watch_codex_threads())

    async def stop(self) -> None:
        if self.watch_task is None:
            return
        self.watch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.watch_task

    async def watch_codex_threads(self) -> None:
        while True:
            try:
                await self.sync_codex_threads()
            except Exception:
                pass
            await asyncio.sleep(self.poll_interval)

    async def create_session(self, title: str | None = None) -> ChatSession:
        async with self.lock:
            now = utc_now()
            session_id = str(uuid.uuid4())
            session = ChatSession(
                id=session_id,
                title=title.strip() if title and title.strip() else f"Bridge Chat {len(self.sessions) + 1}",
                created_at=now,
                updated_at=now,
                source="bridge",
                data_source="bridge",
                bridge_reply_available=True,
            )
            self.sessions[session.id] = session
            return session

    async def get_session(self, session_id: str) -> ChatSession | None:
        async with self.lock:
            return self.sessions.get(session_id)

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self.lock:
            sessions = sorted(
                self.sessions.values(),
                key=lambda item: (item.desktop_thread, item.updated_at),
                reverse=True,
            )
            return [session.summary() for session in sessions]

    async def list_projects(self) -> list[dict[str, str]]:
        if not self.threads_db_path.exists():
            return []
        return await asyncio.to_thread(read_recent_projects_from_sqlite, self.threads_db_path, self.scan_limit)

    async def list_board_runtime_sessions(self, board_root: Path) -> list[dict[str, Any]]:
        live_sessions = await self.list_sessions()
        config = await asyncio.to_thread(read_board_config, board_root)
        target_repo_root = str(config.get("target_repo")) if isinstance(config, dict) and config.get("target_repo") else None
        worktree_root = str(config.get("worktree_root")) if isinstance(config, dict) and config.get("worktree_root") else None

        merged: dict[str, dict[str, Any]] = {}
        if self.threads_db_path.exists():
            historical = await asyncio.to_thread(
                read_board_runtime_sessions_from_sqlite,
                self.threads_db_path,
                max(self.scan_limit * 25, 1500),
                self.allowed_sources,
                str(board_root),
                target_repo_root,
                worktree_root,
            )
            merged.update({item["id"]: item for item in historical if isinstance(item.get("id"), str)})

        for session in live_sessions:
            session_id = session.get("id")
            if isinstance(session_id, str):
                existing = merged.get(session_id, {})
                merged[session_id] = {
                    **existing,
                    **session,
                    "first_user_message": session.get("first_user_message") or existing.get("first_user_message"),
                }

        return list(merged.values())

    async def list_board_folders(self) -> list[dict[str, Any]]:
        recent_project_paths: list[str] = []
        if self.threads_db_path.exists():
            recent_projects = await asyncio.to_thread(read_recent_projects_from_sqlite, self.threads_db_path, self.scan_limit)
            recent_project_paths = [str(item.get("path") or "") for item in recent_projects]

        folders = await asyncio.to_thread(
            discover_board_folders,
            self.workspace,
            self.board_folders,
            self.board_roots,
            recent_project_paths,
        )

        serialized: list[dict[str, Any]] = []
        for folder in folders:
            boards = discover_boards_in_folder(folder)
            if not boards:
                continue
            latest_updated = max(
                max(
                    path.stat().st_mtime
                    for path in (board / "TASK_BOARD.md", board / "THREADS.json", board / "COMM_LOG.md")
                    if path.exists()
                )
                for board in boards
            )
            serialized.append(
                {
                    "id": str(folder),
                    "name": folder.name or str(folder),
                    "path": str(folder),
                    "board_count": len(boards),
                    "updated_at": unix_to_iso(latest_updated),
                }
            )

        serialized.sort(key=lambda item: item["updated_at"], reverse=True)
        return serialized

    async def add_message(self, session: ChatSession, role: str, text: str, state: str = "done") -> ChatMessage:
        async with self.lock:
            message = ChatMessage(
                id=str(uuid.uuid4()),
                role=role,
                text=text,
                created_at=utc_now(),
                state=state,
            )
            session.messages.append(message)
            session.updated_at = message.created_at
            if role == "user" and session.title.startswith("Bridge Chat "):
                session.title = text.strip().splitlines()[0][:48] or session.title
            return message

    async def publish(self, session: ChatSession, event_type: str, payload: dict[str, Any]) -> SessionEvent:
        async with self.lock:
            event = SessionEvent(
                seq=session.next_seq,
                type=event_type,
                timestamp=utc_now(),
                payload=payload,
            )
            session.next_seq += 1
            session.events.append(event)
            session.events = session.events[-500:]
            subscribers = list(session.subscribers)
        for queue in subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        return event

    async def subscribe(self, session: ChatSession) -> asyncio.Queue[SessionEvent]:
        queue: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=128)
        async with self.lock:
            session.subscribers.add(queue)
        return queue

    async def unsubscribe(self, session: ChatSession, queue: asyncio.Queue[SessionEvent]) -> None:
        async with self.lock:
            session.subscribers.discard(queue)

    async def sync_codex_threads(self) -> None:
        if not self.threads_db_path.exists():
            return

        async with self.sync_lock:
            records = await asyncio.to_thread(
                read_threads_from_sqlite,
                self.threads_db_path,
                self.scan_limit,
                self.allowed_sources,
            )

            for record in records:
                await self._upsert_thread_record(record)

    async def _upsert_thread_record(self, record: ThreadRecord) -> None:
        session: ChatSession
        metadata_changed = False
        async with self.lock:
            session = self.sessions.get(record.thread_id) or ChatSession(
                id=record.thread_id,
                title=record.title,
                created_at=record.created_at,
                updated_at=record.updated_at,
                thread_id=record.thread_id,
                imported=True,
                desktop_thread=is_desktop_reply_source(record.source_kind),
                data_source="codex-sqlite+rollout",
                rollout_path=record.rollout_path,
                source_kind=record.source_kind,
                git_branch=record.git_branch,
                parent_thread_id=record.parent_thread_id,
                source_depth=record.source_depth,
                agent_nickname=record.agent_nickname,
                agent_role=record.agent_role,
                bridge_reply_available=is_desktop_reply_source(record.source_kind),
            )
            self.sessions[record.thread_id] = session

            previous_summary = (
                session.title,
                session.created_at,
                session.updated_at,
                session.cwd,
                session.source,
                session.source_kind,
                session.git_branch,
                session.rollout_path,
                session.model_provider,
                session.cli_version,
                session.first_user_message,
                session.parent_thread_id,
                session.source_depth,
                session.agent_nickname,
                session.agent_role,
            )
            session.title = record.title
            session.created_at = record.created_at
            session.updated_at = record.updated_at
            session.thread_id = record.thread_id
            session.cwd = record.cwd
            session.project_root, session.project_name = infer_project_identity(record.cwd)
            session.source = record.source
            session.source_kind = record.source_kind
            session.git_branch = record.git_branch
            session.imported = True
            session.desktop_thread = is_desktop_reply_source(record.source_kind)
            session.data_source = "codex-sqlite+rollout"
            session.rollout_path = record.rollout_path
            session.model_provider = record.model_provider
            session.cli_version = record.cli_version
            session.first_user_message = record.first_user_message
            session.parent_thread_id = record.parent_thread_id
            session.source_depth = record.source_depth
            session.agent_nickname = record.agent_nickname
            session.agent_role = record.agent_role
            session.bridge_reply_available = is_desktop_reply_source(record.source_kind)
            if not session.messages and record.first_user_message and session.title.startswith("Thread "):
                session.title = record.first_user_message.splitlines()[0][:48]
            metadata_changed = previous_summary != (
                session.title,
                session.created_at,
                session.updated_at,
                session.cwd,
                session.source,
                session.source_kind,
                session.git_branch,
                session.rollout_path,
                session.model_provider,
                session.cli_version,
                session.first_user_message,
                session.parent_thread_id,
                session.source_depth,
                session.agent_nickname,
                session.agent_role,
            )

        rollout_path = Path(record.rollout_path).expanduser()
        if not rollout_path.exists():
            if metadata_changed:
                await self.publish(session, "session.synced", {"session": session.summary()})
            return

        should_reset = rollout_path.stat().st_size < session.last_read_offset
        if (
            not should_reset
            and session.last_disk_mtime_ns == rollout_path.stat().st_mtime_ns
            and session.last_disk_size == rollout_path.stat().st_size
        ):
            if metadata_changed:
                await self.publish(session, "session.synced", {"session": session.summary()})
            return

        delta = await asyncio.to_thread(
            parse_rollout_delta,
            rollout_path,
            record.thread_id,
            session.last_read_offset,
            session.last_line_number,
            "" if should_reset else session.pending_fragment,
            {} if should_reset else session.tool_call_names,
            session.running,
            should_reset or session.last_read_offset == 0,
        )
        await self._apply_rollout_delta(session, delta)

    async def _apply_rollout_delta(self, session: ChatSession, delta: ParsedFileDelta) -> None:
        async with self.lock:
            old_running = session.running
            old_message_ids = {message.id for message in session.messages}

            if delta.reset_applied:
                session.messages = []
                old_message_ids = set()

            if delta.created_at:
                session.created_at = delta.created_at
            if delta.updated_at:
                session.updated_at = delta.updated_at
            if delta.cwd:
                session.cwd = delta.cwd
                session.project_root, session.project_name = infer_project_identity(delta.cwd)
            if delta.source:
                session.source = delta.source
            if delta.source_kind:
                session.source_kind = delta.source_kind
            if delta.originator:
                session.originator = delta.originator
            if delta.model_provider:
                session.model_provider = delta.model_provider
            if delta.cli_version:
                session.cli_version = delta.cli_version
            if delta.parent_thread_id:
                session.parent_thread_id = delta.parent_thread_id
            if delta.source_depth is not None:
                session.source_depth = delta.source_depth
            if delta.agent_nickname:
                session.agent_nickname = delta.agent_nickname
            if delta.agent_role:
                session.agent_role = delta.agent_role

            new_messages: list[ChatMessage] = []
            for message in delta.messages:
                if message.id in old_message_ids:
                    continue
                session.messages.append(message)
                new_messages.append(message)

            session.running = delta.running if session.active_task is None else True
            session.last_read_offset = delta.next_offset
            session.last_line_number = delta.next_line_number
            session.pending_fragment = delta.pending_fragment
            session.last_disk_size = delta.stat_size
            session.last_disk_mtime_ns = delta.stat_mtime_ns
            session.messages.sort(key=lambda item: item.created_at)
            session.tool_call_names = dict(list(delta.tool_call_names.items())[-256:])

        for message in new_messages:
            await self.publish(session, role_event_type(message.role), {"message": asdict(message)})
        if old_running != session.running:
            await self.publish(session, "run.state", {"running": session.running})
        if new_messages or delta.reset_applied:
            await self.publish(session, "session.synced", {"session": session.summary()})


async def run_codex(
    session: ChatSession,
    store: SessionStore,
    prompt: str,
    cwd: Path,
    model: str | None,
    access_mode: str | None,
    image_paths: list[Path],
) -> None:
    session.running = True
    session.cwd = str(cwd)
    session.project_root, session.project_name = infer_project_identity(session.cwd)
    resolved_access_mode = normalize_access_mode(access_mode)
    await store.publish(
        session,
        "run.started",
        {
            "cwd": str(cwd),
            "model": model,
            "access_mode": resolved_access_mode,
            "image_count": len(image_paths),
        },
    )
    temp_file = Path(tempfile.mkstemp(prefix=f"{session.id}-", suffix=".txt")[1])

    try:
        command = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--output-last-message",
            str(temp_file),
        ]
        if model:
            command.extend(["--model", model])
        if resolved_access_mode == "danger-full-access":
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["--sandbox", resolved_access_mode])
        for image_path in image_paths:
            command.extend(["--image", str(image_path)])
        if session.thread_id:
            command.extend(["resume", session.thread_id, "-"])
        else:
            command.append("-")

        await store.publish(session, "run.command", {"argv": command})

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        if process.stdin is not None:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            payload = maybe_parse_json(line)
            if payload is not None:
                await store.publish(session, "run.event", payload)
                if payload.get("type") == "thread.started" and isinstance(payload.get("thread_id"), str):
                    session.thread_id = payload["thread_id"]
                    await store.publish(session, "session.thread", {"thread_id": session.thread_id})
            elif line:
                await store.publish(session, "run.output", {"line": line})

        return_code = await process.wait()
        await store.sync_codex_threads()
        text = temp_file.read_text("utf-8").strip() if temp_file.exists() else ""

        if return_code == 0 and not text:
            await store.publish(session, "run.finished", {"return_code": return_code, "empty": True})
        elif return_code != 0:
            message = await store.add_message(session, "system", text or f"codex exited with status {return_code}")
            await store.publish(session, "run.failed", {"return_code": return_code, "message": asdict(message)})
    except Exception as exc:  # pragma: no cover
        message = await store.add_message(session, "system", f"Bridge error: {exc}")
        await store.publish(session, "run.failed", {"return_code": -1, "message": asdict(message)})
    finally:
        session.running = False
        session.active_task = None
        with contextlib.suppress(FileNotFoundError):
            temp_file.unlink()
        await store.sync_codex_threads()
        await store.publish(session, "run.state", {"running": session.running})


async def handle_health(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    board_folders = await store.list_board_folders()
    board_count = sum(int(item["board_count"]) for item in board_folders)
    return json_response(
        {
            "ok": True,
            "timestamp": utc_now(),
            "session_count": len(store.sessions),
            "board_count": board_count,
            "allowed_sources": sorted(store.allowed_sources),
        }
    )


async def handle_root(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    board_folders = await store.list_board_folders()
    board_count = sum(int(item["board_count"]) for item in board_folders)
    return json_response(
        {
            "name": "CodeX Mobile Bridge",
            "allowed_sources": sorted(store.allowed_sources),
            "session_count": len(store.sessions),
            "board_count": board_count,
            "routes": [
                "/healthz",
                "/api/sessions",
                "/api/projects",
                "/api/board-folders",
                "/api/sessions/{id}",
                "/api/sessions/{id}/messages",
                "/api/sessions/{id}/events",
                "/api/boards",
                "/api/boards/{id}",
            ],
        }
    )


async def handle_list_sessions(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    return json_response({"sessions": await store.list_sessions()})


async def handle_list_projects(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    return json_response({"projects": await store.list_projects()})


async def handle_create_session(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    payload = await request.json() if request.can_read_body else {}
    title = payload.get("title") if isinstance(payload, dict) else None
    session = await store.create_session(title=title)
    await store.publish(session, "session.created", {"session": session.summary()})
    return json_response(session.detail(), status=201)


async def handle_get_session(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    session = await store.get_session(request.match_info["session_id"])
    if session is None:
        return json_response({"error": "session not found"}, status=404)
    return json_response(session.detail())


async def handle_send_message(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    session = await store.get_session(request.match_info["session_id"])
    if session is None:
        return json_response({"error": "session not found"}, status=404)
    if session.running:
        return json_response({"error": "session is already running"}, status=409)

    payload: dict[str, Any]
    attachment_fields: list[Any] = []
    if request.content_type.startswith("multipart/form-data"):
        form = await request.post()
        payload = {key: form.get(key) for key in form.keys()}
        for field_name in ("attachments", "attachment", "files", "file", "images", "image"):
            for value in form.getall(field_name, []):
                if hasattr(value, "file") and hasattr(value, "filename"):
                    attachment_fields.append(value)
    else:
        payload = await request.json()
        if not isinstance(payload, dict):
            return json_response({"error": "invalid json body"}, status=400)

    text = str(payload.get("text", "")).strip()
    model = str(payload.get("model")).strip() if payload.get("model") else None
    access_mode = normalize_access_mode(str(payload.get("access_mode", "")).strip() or None)
    cwd_value = str(payload.get("cwd")).strip() if payload.get("cwd") else (session.cwd or str(store.workspace))
    cwd = Path(cwd_value).expanduser().resolve()

    if not text and not attachment_fields:
        return json_response({"error": "text or attachments are required"}, status=400)
    if not cwd.exists():
        return json_response({"error": f"cwd not found: {cwd}"}, status=400)
    if session.imported and not session.desktop_thread:
        return json_response({"error": "reply is only enabled for desktop threads"}, status=400)

    attachments = save_mobile_uploads(attachment_fields, cwd, session.id) if attachment_fields else []
    image_paths = [Path(item["path"]) for item in attachments if item["is_image"]]
    prompt = build_mobile_prompt(text, attachments)

    session.active_task = asyncio.create_task(
        run_codex(session, store, prompt, cwd, model, access_mode, image_paths)
    )
    return json_response({"accepted": True, "session": session.summary()}, status=202)


async def handle_events(request: web.Request) -> web.StreamResponse:
    store: SessionStore = request.app["store"]
    session = await store.get_session(request.match_info["session_id"])
    if session is None:
        return web.Response(status=404, text="session not found")

    try:
        after = int(request.query.get("after", "0"))
    except ValueError:
        after = 0

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await response.prepare(request)

    async def send_event(event: SessionEvent) -> None:
        data = {
            "seq": event.seq,
            "type": event.type,
            "timestamp": event.timestamp,
            "session_id": session.id,
            "payload": event.payload,
        }
        chunk = f"id: {event.seq}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
        await response.write(chunk)

    backlog = [event for event in session.events if event.seq > after]
    for event in backlog:
        await send_event(event)

    queue = await store.subscribe(session)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                await send_event(event)
            except asyncio.TimeoutError:
                await response.write(b": ping\n\n")
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass
    finally:
        await store.unsubscribe(session, queue)
        with contextlib.suppress(ConnectionResetError, RuntimeError):
            await response.write_eof()
    return response


async def handle_list_boards(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    folder_query = (request.query.get("folder") or "").strip()
    if folder_query:
        scan_folders = [Path(folder_query).expanduser().resolve()]
    else:
        scan_folders = [Path(item["path"]) for item in await store.list_board_folders()]
    boards: list[dict[str, Any]] = []
    seen_board_ids: set[str] = set()
    for scan_folder in scan_folders:
        for board_root in discover_boards_in_folder(scan_folder):
            snapshot = read_board_snapshot(
                board_root,
                session_summaries=await store.list_board_runtime_sessions(board_root),
            )
            if snapshot["id"] in seen_board_ids:
                continue
            seen_board_ids.add(snapshot["id"])
            boards.append(
                {
                    "id": snapshot["id"],
                    "title": snapshot["title"],
                    "path": snapshot["path"],
                    "folder_path": snapshot["folder_path"],
                    "updated_at": snapshot["updated_at"],
                    "task_count": snapshot["task_count"],
                    "thread_count": snapshot["thread_count"],
                    "totals": snapshot["totals"],
                }
            )
    boards.sort(key=lambda item: item["updated_at"], reverse=True)
    return json_response({"boards": boards})


async def handle_list_board_folders(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    return json_response({"folders": await store.list_board_folders()})


async def handle_get_board(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    board_id = request.match_info["board_id"]
    for folder in await store.list_board_folders():
        for board_root in discover_boards_in_folder(Path(folder["path"])):
            if board_id_for_path(board_root) == board_id:
                return json_response(
                    read_board_snapshot(
                        board_root,
                        session_summaries=await store.list_board_runtime_sessions(board_root),
                    )
                )
    return json_response({"error": "board not found"}, status=404)


async def on_startup(app: web.Application) -> None:
    store: SessionStore = app["store"]
    await store.start()


async def on_cleanup(app: web.Application) -> None:
    store: SessionStore = app["store"]
    await store.stop()


def create_app() -> web.Application:
    workspace = Path(os.environ.get("CODEX_BRIDGE_CWD", os.getcwd())).expanduser().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    threads_db_path = Path(os.environ.get("CODEX_BRIDGE_THREADS_DB", codex_home / "state_5.sqlite")).expanduser().resolve()
    scan_limit = int(os.environ.get("CODEX_BRIDGE_IMPORT_LIMIT", "60"))
    poll_interval = float(os.environ.get("CODEX_BRIDGE_POLL_SECONDS", "2"))
    allowed_sources = {
        item.strip()
        for item in os.environ.get("CODEX_BRIDGE_ALLOWED_SOURCES", DEFAULT_ALLOWED_SOURCE_KINDS).split(",")
        if item.strip()
    }
    board_roots = tuple(
        Path(item.strip()).expanduser().resolve()
        for item in os.environ.get("CODEX_BRIDGE_BOARD_ROOTS", "").split(",")
        if item.strip()
    )
    board_folders = tuple(
        Path(item.strip()).expanduser().resolve()
        for item in os.environ.get("CODEX_BRIDGE_BOARD_FOLDERS", "").split(",")
        if item.strip()
    )

    store = SessionStore(
        workspace=workspace,
        codex_home=codex_home,
        threads_db_path=threads_db_path,
        scan_limit=scan_limit,
        poll_interval=poll_interval,
        allowed_sources=allowed_sources,
        board_folders=board_folders,
        board_roots=board_roots,
    )

    app = web.Application()
    app["store"] = store
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/sessions", handle_list_sessions)
    app.router.add_get("/api/projects", handle_list_projects)
    app.router.add_get("/api/board-folders", handle_list_board_folders)
    app.router.add_post("/api/sessions", handle_create_session)
    app.router.add_get("/api/sessions/{session_id}", handle_get_session)
    app.router.add_post("/api/sessions/{session_id}/messages", handle_send_message)
    app.router.add_get("/api/sessions/{session_id}/events", handle_events)
    app.router.add_get("/api/boards", handle_list_boards)
    app.router.add_get("/api/boards/{board_id}", handle_get_board)
    return app


if __name__ == "__main__":
    application = create_app()
    host = os.environ.get("CODEX_BRIDGE_BIND", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    web.run_app(application, host=host, port=port)
