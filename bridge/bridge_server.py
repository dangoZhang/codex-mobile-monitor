from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web


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
class ChatSession:
    id: str
    title: str
    created_at: str
    updated_at: str
    thread_id: str | None = None
    cwd: str | None = None
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
    ) -> None:
        self.workspace = workspace
        self.codex_home = codex_home
        self.threads_db_path = threads_db_path
        self.scan_limit = scan_limit
        self.poll_interval = poll_interval
        self.allowed_sources = allowed_sources
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
    return json_response(
        {
            "ok": True,
            "timestamp": utc_now(),
            "session_count": len(store.sessions),
            "threads_db_path": str(store.threads_db_path),
            "allowed_sources": sorted(store.allowed_sources),
        }
    )


async def handle_root(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    return json_response(
        {
            "name": "CodeX Mobile Bridge",
            "workspace": str(store.workspace),
            "codex_home": str(store.codex_home),
            "threads_db_path": str(store.threads_db_path),
            "allowed_sources": sorted(store.allowed_sources),
            "session_count": len(store.sessions),
            "routes": [
                "/healthz",
                "/api/sessions",
                "/api/sessions/{id}",
                "/api/sessions/{id}/messages",
                "/api/sessions/{id}/events",
            ],
        }
    )


async def handle_list_sessions(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    return json_response({"sessions": await store.list_sessions()})


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

    store = SessionStore(
        workspace=workspace,
        codex_home=codex_home,
        threads_db_path=threads_db_path,
        scan_limit=scan_limit,
        poll_interval=poll_interval,
        allowed_sources=allowed_sources,
    )

    app = web.Application()
    app["store"] = store
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/sessions", handle_list_sessions)
    app.router.add_post("/api/sessions", handle_create_session)
    app.router.add_get("/api/sessions/{session_id}", handle_get_session)
    app.router.add_post("/api/sessions/{session_id}/messages", handle_send_message)
    app.router.add_get("/api/sessions/{session_id}/events", handle_events)
    return app


if __name__ == "__main__":
    application = create_app()
    host = os.environ.get("CODEX_BRIDGE_BIND", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    web.run_app(application, host=host, port=port)
