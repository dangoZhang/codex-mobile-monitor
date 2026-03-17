from __future__ import annotations

import asyncio
import contextlib
import json
import os
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
        if name == "apply_patch":
            return "Applied patch to workspace files"
        if name.startswith("mcp__playwright__"):
            action = name.removeprefix("mcp__playwright__").replace("_", " ")
            element = str(parsed.get("element") or "")
            return compact_text(f"{action}\n{element}" if element else action)

    return compact_text(name)


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
    cwd: str
    created_at: str
    updated_at: str
    rollout_path: str
    model_provider: str | None
    cli_version: str | None
    first_user_message: str | None


@dataclass
class ParsedFileDelta:
    messages: list[ChatMessage]
    created_at: str | None
    updated_at: str | None
    cwd: str | None
    source: str | None
    originator: str | None
    model_provider: str | None
    cli_version: str | None
    running: bool
    next_offset: int
    next_line_number: int
    pending_fragment: str
    stat_size: int
    stat_mtime_ns: int
    reset_applied: bool


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
    originator: str | None = None
    imported: bool = False
    desktop_thread: bool = False
    data_source: str = "bridge"
    rollout_path: str | None = None
    model_provider: str | None = None
    cli_version: str | None = None
    bridge_reply_available: bool = True
    messages: list[ChatMessage] = field(default_factory=list)
    events: list[SessionEvent] = field(default_factory=list)
    subscribers: set[asyncio.Queue[SessionEvent]] = field(default_factory=set)
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
            "originator": self.originator,
            "imported": self.imported,
            "desktop_thread": self.desktop_thread,
            "data_source": self.data_source,
            "rollout_path": self.rollout_path,
            "model_provider": self.model_provider,
            "cli_version": self.cli_version,
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
            SELECT id, title, source, cwd, created_at, updated_at, rollout_path, model_provider, cli_version, first_user_message
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
            source = str(row["source"] or "")
            if source not in allowed_sources:
                continue
            thread_id = str(row["id"])
            if thread_id in seen_ids:
                continue
            seen_ids.add(thread_id)
            records.append(
                ThreadRecord(
                    thread_id=thread_id,
                    title=str(row["title"] or row["first_user_message"] or f"Thread {thread_id[:8]}"),
                    source=source,
                    cwd=str(row["cwd"] or ""),
                    created_at=unix_to_iso(row["created_at"]),
                    updated_at=unix_to_iso(row["updated_at"]),
                    rollout_path=str(row["rollout_path"] or ""),
                    model_provider=str(row["model_provider"]) if row["model_provider"] else None,
                    cli_version=str(row["cli_version"]) if row["cli_version"] else None,
                    first_user_message=str(row["first_user_message"]) if row["first_user_message"] else None,
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


def is_board_root(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "TASK_BOARD.md").exists()
        and (path / "THREADS.json").exists()
        and (path / "COMM_LOG.md").exists()
    )


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


def build_board_repo_snapshot(
    board_root: Path,
    thread_defs: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], str | None, str | None]:
    config_path = board_root / "coordination.config.json"
    if not config_path.exists():
        return None, {}, None, None

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"configured": False, "error": "invalid coordination.config.json"}, {}, None, None

    if not isinstance(raw, dict):
        return {"configured": False, "error": "invalid coordination.config.json"}, {}, None, None

    target_repo_raw = raw.get("target_repo")
    base_branch = str(raw.get("base_branch") or "") or None
    target_repo = Path(str(target_repo_raw)).expanduser().resolve() if target_repo_raw else None
    if target_repo is None:
        return {"configured": False, "error": "missing target_repo"}, {}, base_branch, None
    if not target_repo.exists():
        return {
            "configured": False,
            "error": f"target repo not found: {target_repo}",
        }, {}, base_branch, str(target_repo)

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
    }


def read_board_snapshot(board_root: Path) -> dict[str, Any]:
    task_board_path = board_root / "TASK_BOARD.md"
    threads_path = board_root / "THREADS.json"
    comm_log_path = board_root / "COMM_LOG.md"
    thread_defs = load_board_threads(threads_path)
    tasks = parse_board_tasks(task_board_path)
    logs = parse_board_comm_log(comm_log_path)
    title = parse_board_title(board_root)
    repo_snapshot, branch_snapshots, base_branch, target_repo_root = build_board_repo_snapshot(board_root, thread_defs)
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
                    )
                    if selected_task is not None
                    else None
                ),
                "last_log": logs["latest"].get(thread_id),
                "runtime_start": logs["kickoff_latest"].get(thread_id),
                "last_invocation": logs["last_invocation"].get(thread_id),
                "branches": branch_snapshots.get(thread_id),
            }
        )

    updated_at = max(
        path.stat().st_mtime
        for path in (task_board_path, threads_path, comm_log_path)
        if path.exists()
    )
    return {
        "id": board_root.name,
        "title": title,
        "path": str(board_root),
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
    originator: str | None = None
    model_provider: str | None = None
    cli_version: str | None = None
    messages: list[ChatMessage] = []

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

        if item_type == "function_call":
            name = str(item.get("name") or "tool")
            text = summarize_tool_call(name, item.get("arguments"))
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

    return ParsedFileDelta(
        messages=messages,
        created_at=created_at,
        updated_at=updated_at,
        cwd=cwd,
        source=source,
        originator=originator,
        model_provider=model_provider,
        cli_version=cli_version,
        running=running,
        next_offset=stat_result.st_size,
        next_line_number=line_number,
        pending_fragment=fragment,
        stat_size=stat_result.st_size,
        stat_mtime_ns=stat_result.st_mtime_ns,
        reset_applied=reset,
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
        board_roots: tuple[Path, ...],
    ) -> None:
        self.workspace = workspace
        self.codex_home = codex_home
        self.threads_db_path = threads_db_path
        self.scan_limit = scan_limit
        self.poll_interval = poll_interval
        self.allowed_sources = allowed_sources
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
        async with self.lock:
            session = self.sessions.get(record.thread_id) or ChatSession(
                id=record.thread_id,
                title=record.title,
                created_at=record.created_at,
                updated_at=record.updated_at,
                thread_id=record.thread_id,
                imported=True,
                desktop_thread=True,
                data_source="codex-sqlite+rollout",
                rollout_path=record.rollout_path,
                bridge_reply_available=True,
            )
            self.sessions[record.thread_id] = session

            session.title = record.title
            session.created_at = record.created_at
            session.updated_at = record.updated_at
            session.thread_id = record.thread_id
            session.cwd = record.cwd
            session.project_root, session.project_name = infer_project_identity(record.cwd)
            session.source = record.source
            session.imported = True
            session.desktop_thread = True
            session.data_source = "codex-sqlite+rollout"
            session.rollout_path = record.rollout_path
            session.model_provider = record.model_provider
            session.cli_version = record.cli_version
            if not session.messages and record.first_user_message and session.title.startswith("Thread "):
                session.title = record.first_user_message.splitlines()[0][:48]

        rollout_path = Path(record.rollout_path).expanduser()
        if not rollout_path.exists():
            return

        should_reset = rollout_path.stat().st_size < session.last_read_offset
        if (
            not should_reset
            and session.last_disk_mtime_ns == rollout_path.stat().st_mtime_ns
            and session.last_disk_size == rollout_path.stat().st_size
        ):
            return

        delta = await asyncio.to_thread(
            parse_rollout_delta,
            rollout_path,
            record.thread_id,
            session.last_read_offset,
            session.last_line_number,
            "" if should_reset else session.pending_fragment,
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
            if delta.originator:
                session.originator = delta.originator
            if delta.model_provider:
                session.model_provider = delta.model_provider
            if delta.cli_version:
                session.cli_version = delta.cli_version

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

        for message in new_messages:
            await self.publish(session, role_event_type(message.role), {"message": asdict(message)})
        if old_running != session.running:
            await self.publish(session, "run.state", {"running": session.running})
        if new_messages or delta.reset_applied:
            await self.publish(session, "session.synced", {"session": session.summary()})


async def run_codex(session: ChatSession, store: SessionStore, prompt: str, cwd: Path, model: str | None) -> None:
    session.running = True
    session.cwd = str(cwd)
    session.project_root, session.project_name = infer_project_identity(session.cwd)
    await store.publish(session, "run.started", {"cwd": str(cwd), "model": model})
    temp_file = Path(tempfile.mkstemp(prefix=f"{session.id}-", suffix=".txt")[1])

    try:
        command = ["codex", "exec"]
        if session.thread_id:
            command.extend(["resume", session.thread_id])
        command.extend(
            [
                "--json",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
                "--output-last-message",
                str(temp_file),
            ]
        )
        if model:
            command.extend(["--model", model])
        command.append(prompt)

        await store.publish(session, "run.command", {"argv": command})

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

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
    boards = discover_board_roots(store.workspace, store.board_roots)
    return json_response(
        {
            "ok": True,
            "timestamp": utc_now(),
            "session_count": len(store.sessions),
            "board_count": len(boards),
            "allowed_sources": sorted(store.allowed_sources),
        }
    )


async def handle_root(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    boards = discover_board_roots(store.workspace, store.board_roots)
    return json_response(
        {
            "name": "CodeX Mobile Bridge",
            "allowed_sources": sorted(store.allowed_sources),
            "session_count": len(store.sessions),
            "board_count": len(boards),
            "routes": [
                "/healthz",
                "/api/sessions",
                "/api/projects",
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

    payload = await request.json()
    if not isinstance(payload, dict):
        return json_response({"error": "invalid json body"}, status=400)

    text = str(payload.get("text", "")).strip()
    model = str(payload.get("model")).strip() if payload.get("model") else None
    cwd_value = str(payload.get("cwd")).strip() if payload.get("cwd") else (session.cwd or str(store.workspace))
    cwd = Path(cwd_value).expanduser().resolve()

    if not text:
        return json_response({"error": "text is required"}, status=400)
    if not cwd.exists():
        return json_response({"error": f"cwd not found: {cwd}"}, status=400)
    if session.imported and not session.desktop_thread:
        return json_response({"error": "reply is only enabled for desktop threads"}, status=400)

    session.active_task = asyncio.create_task(run_codex(session, store, text, cwd, model))
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
    boards: list[dict[str, Any]] = []
    for board_root in discover_board_roots(store.workspace, store.board_roots):
        snapshot = read_board_snapshot(board_root)
        boards.append(
            {
                "id": snapshot["id"],
                "title": snapshot["title"],
                "path": snapshot["path"],
                "updated_at": snapshot["updated_at"],
                "task_count": snapshot["task_count"],
                "thread_count": snapshot["thread_count"],
                "totals": snapshot["totals"],
            }
        )
    boards.sort(key=lambda item: item["updated_at"], reverse=True)
    return json_response({"boards": boards})


async def handle_get_board(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    board_id = request.match_info["board_id"]
    for board_root in discover_board_roots(store.workspace, store.board_roots):
        if board_root.name == board_id:
            return json_response(read_board_snapshot(board_root))
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
        for item in os.environ.get("CODEX_BRIDGE_ALLOWED_SOURCES", "vscode,app").split(",")
        if item.strip()
    }
    board_roots = tuple(
        Path(item.strip()).expanduser().resolve()
        for item in os.environ.get("CODEX_BRIDGE_BOARD_ROOTS", "").split(",")
        if item.strip()
    )

    store = SessionStore(
        workspace=workspace,
        codex_home=codex_home,
        threads_db_path=threads_db_path,
        scan_limit=scan_limit,
        poll_interval=poll_interval,
        allowed_sources=allowed_sources,
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
