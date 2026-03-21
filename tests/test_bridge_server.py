from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from bridge.bridge_server import (
    ChatSession,
    SessionStore,
    annotate_runtime_snapshot,
    build_board_runtime_index,
    extract_runtime_thread_id,
    parse_rollout_delta,
    read_board_runtime_sessions_from_sqlite,
    read_threads_from_sqlite,
    resolve_codex_command,
)


class BridgeServerTests(unittest.TestCase):
    def _create_threads_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                sandbox_policy TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                has_user_event INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                archived_at INTEGER,
                git_sha TEXT,
                git_branch TEXT,
                git_origin_url TEXT,
                cli_version TEXT NOT NULL DEFAULT '',
                first_user_message TEXT NOT NULL DEFAULT '',
                agent_nickname TEXT,
                agent_role TEXT,
                memory_mode TEXT NOT NULL DEFAULT 'enabled',
                model TEXT,
                reasoning_effort TEXT
            )
            """
        )

    def test_read_threads_from_sqlite_imports_subagent_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "state.sqlite"
            connection = sqlite3.connect(db_path)
            self._create_threads_table(connection)
            source = json.dumps(
                {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "parent-thread",
                            "depth": 1,
                            "agent_nickname": "Euler",
                            "agent_role": "worker",
                        }
                    }
                },
                ensure_ascii=False,
            )
            connection.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived,
                    archived_at, git_sha, git_branch, git_origin_url, cli_version,
                    first_user_message, agent_nickname, agent_role, memory_mode, model, reasoning_effort
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "child-thread",
                    "/tmp/child.jsonl",
                    1774054800,
                    1774054801,
                    source,
                    "openai_http",
                    "/tmp/project",
                    "Original title",
                    "workspace-write",
                    "never",
                    0,
                    1,
                    0,
                    None,
                    None,
                    None,
                    None,
                    "0.200.0",
                    "inspect src",
                    "Euler",
                    "worker",
                    "enabled",
                    "gpt-5.4",
                    "medium",
                ),
            )
            connection.commit()
            connection.close()

            records = read_threads_from_sqlite(db_path, limit=10, allowed_sources={"subagent"})

            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record.source_kind, "subagent")
            self.assertEqual(record.parent_thread_id, "parent-thread")
            self.assertEqual(record.agent_nickname, "Euler")
            self.assertEqual(record.agent_role, "worker")
            self.assertEqual(record.title, "Euler (worker)")

    def test_parse_rollout_delta_captures_tool_outputs_and_web_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            rollout_path = Path(tmp_dir) / "rollout.jsonl"
            lines = [
                {
                    "timestamp": "2026-03-21T01:00:00.000Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "child-thread",
                        "forked_from_id": "parent-thread",
                        "timestamp": "2026-03-21T01:00:00.000Z",
                        "cwd": "/tmp/project",
                        "originator": "Codex Desktop",
                        "cli_version": "0.200.0",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": "parent-thread",
                                    "depth": 1,
                                    "agent_nickname": "Euler",
                                    "agent_role": "worker",
                                }
                            }
                        },
                        "agent_nickname": "Euler",
                        "agent_role": "worker",
                        "model_provider": "openai_http",
                    },
                },
                {
                    "timestamp": "2026-03-21T01:00:01.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "wait_agent",
                        "arguments": json.dumps({"ids": ["child-thread"], "timeout_ms": 1000}),
                        "call_id": "call-wait",
                    },
                },
                {
                    "timestamp": "2026-03-21T01:00:02.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-wait",
                        "output": json.dumps({"status": {"completed": "done"}, "timed_out": False}),
                    },
                },
                {
                    "timestamp": "2026-03-21T01:00:03.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "web_search_call",
                        "status": "completed",
                        "action": {"type": "search", "query": "codex subagent"},
                    },
                },
            ]
            rollout_path.write_text(
                "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
                encoding="utf-8",
            )

            delta = parse_rollout_delta(
                rollout_path,
                "child-thread",
                start_offset=0,
                start_line_number=0,
                pending_fragment="",
                start_tool_call_names={},
                initial_running=False,
                reset=True,
            )

            self.assertEqual(delta.source_kind, "subagent")
            self.assertEqual(delta.parent_thread_id, "parent-thread")
            self.assertEqual(delta.agent_nickname, "Euler")
            self.assertEqual(delta.agent_role, "worker")
            self.assertEqual([message.tool_name for message in delta.messages], ["wait_agent", "wait_agent", "web_search"])
            self.assertIn("child-thread", delta.messages[0].text)
            self.assertEqual(delta.messages[1].text, '{"completed": "done"}')
            self.assertEqual(delta.messages[2].text, "search\ncodex subagent")

    def test_extract_runtime_thread_id_accepts_resume_events(self) -> None:
        self.assertEqual(
            extract_runtime_thread_id({"type": "thread.resumed", "thread_id": "thread-123"}),
            "thread-123",
        )
        self.assertEqual(
            extract_runtime_thread_id({"type": "session.resumed", "session_id": "thread-456"}),
            "thread-456",
        )
        self.assertIsNone(extract_runtime_thread_id({"type": "response.output_text.delta", "thread_id": "ignored"}))

    def test_resolve_codex_command_prefers_explicit_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CODEX_BRIDGE_CODEX_COMMAND": "/tmp/codex --profile desktop",
                "CODEX_BRIDGE_CODEX_ARGS": "--enable subagents --disable legacy_mode",
            },
            clear=False,
        ):
            command = resolve_codex_command()
        self.assertEqual(
            command,
            (
                "/tmp/codex",
                "--profile",
                "desktop",
                "--enable",
                "subagents",
                "--disable",
                "legacy_mode",
            ),
        )

    def test_read_board_runtime_sessions_from_sqlite_covers_board_parent_and_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            board_root = root / "codex_coordination"
            target_repo = root / "demo"
            parent_worktree = root / ".codex-worktrees" / "bench"
            repo_worktree = target_repo / ".codex-worktrees" / "thread1"
            board_root.mkdir()
            target_repo.mkdir()
            parent_worktree.mkdir(parents=True)
            repo_worktree.mkdir(parents=True)

            db_path = root / "state.sqlite"
            connection = sqlite3.connect(db_path)
            self._create_threads_table(connection)
            rows = [
                (
                    "thread4-run",
                    "/tmp/t4.jsonl",
                    1774054800,
                    1774054801,
                    "exec",
                    "openai_http",
                    str(parent_worktree),
                    "Thread4 benchmark",
                    "workspace-write",
                    "never",
                    0,
                    1,
                    0,
                    None,
                    None,
                    None,
                    None,
                    "0.200.0",
                    "Benchmark case generated by thread4 for a missing capability.",
                    None,
                    None,
                    "enabled",
                    "gpt-5.4",
                    "medium",
                ),
                (
                    "thread1-run",
                    "/tmp/t1.jsonl",
                    1774054800,
                    1774054802,
                    "vscode",
                    "openai_http",
                    str(repo_worktree),
                    "Backend coding",
                    "workspace-write",
                    "never",
                    0,
                    1,
                    0,
                    None,
                    None,
                    "codex/thread1-mainline",
                    None,
                    "0.200.0",
                    "",
                    None,
                    None,
                    "enabled",
                    "gpt-5.4",
                    "medium",
                ),
            ]
            connection.executemany(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived,
                    archived_at, git_sha, git_branch, git_origin_url, cli_version,
                    first_user_message, agent_nickname, agent_role, memory_mode, model, reasoning_effort
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.commit()
            connection.close()

            sessions = read_board_runtime_sessions_from_sqlite(
                db_path,
                limit=20,
                allowed_sources={"vscode", "exec"},
                board_root=str(
                    Path(f"/private{board_root}")
                    if str(board_root).startswith("/var/")
                    else board_root
                ),
                target_repo_root=str(target_repo),
            )

            self.assertEqual({item["id"] for item in sessions}, {"thread4-run", "thread1-run"})

    def test_list_board_runtime_sessions_preserves_historical_match_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            board_root = root / "codex_coordination"
            target_repo = root / "demo"
            board_root.mkdir()
            target_repo.mkdir()

            db_path = root / "state.sqlite"
            connection = sqlite3.connect(db_path)
            self._create_threads_table(connection)
            connection.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived,
                    archived_at, git_sha, git_branch, git_origin_url, cli_version,
                    first_user_message, agent_nickname, agent_role, memory_mode, model, reasoning_effort
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "bench-thread",
                    "/tmp/bench.jsonl",
                    1774054800,
                    1774054900,
                    "exec",
                    "openai_http",
                    str(root / ".codex-worktrees"),
                    "Benchmark worker",
                    "workspace-write",
                    "never",
                    0,
                    1,
                    0,
                    None,
                    None,
                    None,
                    None,
                    "0.200.0",
                    "Benchmark case generated by thread4 for a missing capability.",
                    None,
                    None,
                    "enabled",
                    "gpt-5.4",
                    "medium",
                ),
            )
            connection.commit()
            connection.close()

            (board_root / "coordination.config.json").write_text(
                json.dumps({"target_repo": str(target_repo)}, ensure_ascii=False),
                encoding="utf-8",
            )

            store = SessionStore(
                workspace=root,
                codex_home=root,
                codex_command=("codex",),
                codex_version="codex-cli 0.116.0-alpha.10",
                threads_db_path=db_path,
                scan_limit=60,
                poll_interval=1.0,
                allowed_sources={"exec"},
                board_folders=(root,),
                board_roots=(board_root,),
            )
            store.sessions["bench-thread"] = ChatSession(
                id="bench-thread",
                title="Benchmark worker",
                created_at="2026-03-21T01:00:00.000Z",
                updated_at="2026-03-21T01:01:00.000Z",
                thread_id="bench-thread",
                cwd=str(root / ".codex-worktrees"),
                source="exec",
                source_kind="exec",
                imported=True,
                desktop_thread=False,
                data_source="codex-sqlite+rollout",
                bridge_reply_available=False,
            )

            sessions = asyncio.run(store.list_board_runtime_sessions(board_root))

            self.assertEqual(len(sessions), 1)
            self.assertEqual(
                sessions[0]["first_user_message"],
                "Benchmark case generated by thread4 for a missing capability.",
            )

    def test_read_board_runtime_sessions_from_sqlite_accepts_alias_board_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            actual_root = root / "actual"
            alias_root = root / "alias"
            actual_root.mkdir()
            alias_root.symlink_to(actual_root, target_is_directory=True)

            board_root = actual_root / "codex_coordination"
            target_repo = actual_root / "demo"
            legacy_worktree = actual_root / ".codex-worktrees" / "bench"
            board_root.mkdir()
            target_repo.mkdir()
            legacy_worktree.mkdir(parents=True)

            db_path = root / "state.sqlite"
            connection = sqlite3.connect(db_path)
            self._create_threads_table(connection)
            connection.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived,
                    archived_at, git_sha, git_branch, git_origin_url, cli_version,
                    first_user_message, agent_nickname, agent_role, memory_mode, model, reasoning_effort
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "alias-thread",
                    "/tmp/a.jsonl",
                    1774054800,
                    1774054801,
                    "exec",
                    "openai_http",
                    str(legacy_worktree),
                    "Benchmark worker",
                    "workspace-write",
                    "never",
                    0,
                    1,
                    0,
                    None,
                    None,
                    None,
                    None,
                    "0.200.0",
                    "Benchmark case generated by thread4 for a missing capability.",
                    None,
                    None,
                    "enabled",
                    "gpt-5.4",
                    "medium",
                ),
            )
            connection.commit()
            connection.close()

            sessions = read_board_runtime_sessions_from_sqlite(
                db_path,
                limit=20,
                allowed_sources={"exec"},
                board_root=str(alias_root / "codex_coordination"),
                target_repo_root=str(alias_root / "demo"),
            )

            self.assertEqual([item["id"] for item in sessions], ["alias-thread"])

    def test_build_board_runtime_index_prefers_thread_identity_before_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            board_root = root / "codex_coordination"
            target_repo = root / "demo"
            board_root.mkdir()
            (root / ".codex-worktrees").mkdir()
            (target_repo / ".codex-worktrees").mkdir(parents=True)

            thread_defs = [
                {"id": "thread1", "slot": "01", "name": "01-Backbone", "role": "Backend"},
                {"id": "thread3", "slot": "03", "name": "03-Review", "role": "Review Gate"},
                {"id": "thread4", "slot": "04", "name": "04-Test", "role": "Test / Experiment"},
                {"id": "thread6", "slot": "06", "name": "06-Paper", "role": "Validation / Paper"},
            ]
            sessions = [
                {
                    "id": "thread1-live",
                    "title": "Backend coding",
                    "cwd": str(target_repo / ".codex-worktrees" / "codex__thread1-mainline"),
                    "git_branch": "codex/thread1-mainline",
                    "updated_at": "2026-03-21T01:00:00.000Z",
                    "source_kind": "vscode",
                    "running": True,
                    "last_message_preview": "editing backend",
                },
                {
                    "id": "thread3-live",
                    "title": "You are acting as 03-Review for this repository.",
                    "first_user_message": "Review branch codex/thread1-mainline as 03-Review.",
                    "cwd": str(target_repo),
                    "git_branch": "codex/thread1-mainline",
                    "updated_at": "2026-03-21T01:01:00.000Z",
                    "source_kind": "exec",
                    "running": False,
                    "last_message_preview": "reviewing branch",
                },
                {
                    "id": "thread4-live",
                    "title": "Benchmark worker",
                    "first_user_message": "Benchmark case generated by thread4 for a missing capability.",
                    "cwd": str(root / ".codex-worktrees"),
                    "git_branch": None,
                    "updated_at": "2026-03-21T01:02:00.000Z",
                    "source_kind": "exec",
                    "running": False,
                    "last_message_preview": "benchmark case",
                },
                {
                    "id": "thread6-live",
                    "title": "You are thread6.",
                    "first_user_message": "H-T1-T6-AUTO submitted for thread1 commit abc123.",
                    "cwd": str(target_repo),
                    "git_branch": "codex/thread1-default-skill",
                    "updated_at": "2026-03-21T01:03:00.000Z",
                    "source_kind": "vscode",
                    "running": False,
                    "last_message_preview": "auto dispatch",
                },
            ]

            runtime = build_board_runtime_index(
                board_root,
                str(target_repo),
                None,
                thread_defs,
                sessions,
            )

            self.assertEqual(runtime["thread1"]["latest_title"], "Backend coding")
            self.assertEqual(runtime["thread3"]["latest_title"], "You are acting as 03-Review for this repository.")
            self.assertEqual(runtime["thread4"]["latest_title"], "Benchmark worker")
            self.assertEqual(runtime["thread6"]["latest_title"], "You are thread6.")
            self.assertTrue(runtime["thread1"]["running"])

    def test_annotate_runtime_snapshot_marks_stale_when_board_log_is_newer(self) -> None:
        runtime = {
            "session_count": 1,
            "subagent_count": 0,
            "running": False,
            "updated_at": "2026-03-18T01:00:00.000Z",
        }
        latest_log = {
            "timestamp": "2026-03-19 09:00",
            "type": "update",
            "message": "newer board event",
            "line_no": 12,
        }

        annotated = annotate_runtime_snapshot(runtime, latest_log)

        self.assertIsNotNone(annotated)
        self.assertTrue(annotated["stale"])
        self.assertEqual(annotated["stale_reason"], "log_newer")

    def test_annotate_runtime_snapshot_tolerates_same_day_timezone_offset(self) -> None:
        runtime = {
            "session_count": 1,
            "subagent_count": 0,
            "running": False,
            "updated_at": "2026-03-21T04:40:05.000Z",
        }
        latest_log = {
            "timestamp": "2026-03-21 10:00",
            "type": "kickoff",
            "message": "same-day local log",
            "line_no": 3,
        }

        annotated = annotate_runtime_snapshot(runtime, latest_log)

        self.assertIsNotNone(annotated)
        self.assertFalse(annotated["stale"])
        self.assertIsNone(annotated["stale_reason"])


if __name__ == "__main__":
    unittest.main()
