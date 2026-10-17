"""Fixture-based checks: no credentials, network, or real conversation data."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from poketask.monitor import Monitor


def stamp(offset=0):
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat()


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.codex = self.base / "codex" / "sessions"
        self.claude = self.base / "claude" / "projects"
        self.codex.mkdir(parents=True)
        self.claude.mkdir(parents=True)
        self.monitor = Monitor(self.codex, self.claude)
        self.log = self.codex / "test.jsonl"

    @staticmethod
    def write(path, *rows, mode="a"):
        with path.open(mode, encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def meta(self, identity="test", **extra):
        return {"type": "session_meta", "timestamp": stamp(-20), "payload": {
            "id": identity, "cwd": "C:\\Projects\\Game", "source": "vscode",
            "originator": "codex_vscode", **extra,
        }}

    def event(self, event, offset=0, **extra):
        return {"type": "event_msg", "timestamp": stamp(offset), "payload": {"type": event, **extra}}

    def test_codex_status_usage_and_quota_are_distinct(self):
        self.write(self.log, self.meta(), self.event("task_started", -10), self.event(
            "token_count", -5, info={
                "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 200,
                                      "output_tokens": 100, "total_tokens": 1100},
                "last_token_usage": {"total_tokens": 400}, "model_context_window": 10000,
            }, rate_limits={"primary": {"used_percent": 23, "window_minutes": 300, "resets_at": 1800000000}}
        ))
        session, = self.monitor.poll()
        self.assertEqual((session.provider, session.source, session.state), ("codex", "VS Code", "working"))
        self.assertEqual((session.input_tokens, session.cached_tokens, session.total_tokens), (1000, 200, 1100))
        self.assertEqual(session.context_percent, 4)
        self.assertEqual((session.quota[0].label, session.quota[0].used_percent), ("5 小时", 23))
        self.write(self.log, self.event("task_complete"))
        completed, = self.monitor.poll()
        self.assertEqual(completed.state, "completed")
        self.assertEqual(session.state, "working", "Previously returned snapshots must remain unchanged")
        completed.quota[0].used_percent = 99
        self.assertEqual(self.monitor.poll()[0].quota[0].used_percent, 23)

    def test_half_line_is_buffered_and_invalid_json_is_skipped(self):
        self.write(self.log, self.meta(), self.event("task_started", -10))
        self.assertEqual(self.monitor.poll()[0].state, "working")
        line = json.dumps(self.event("task_complete")).encode()
        with self.log.open("ab") as stream:
            stream.write(b"not-json-secret\n" + line[:23])
        self.assertEqual(self.monitor.poll()[0].state, "working")
        self.assertNotIn("secret", " ".join(self.monitor.errors))
        with self.log.open("ab") as stream:
            stream.write(line[23:] + b"\n")
        self.assertEqual(self.monitor.poll()[0].state, "completed")

    def test_truncation_and_replacement_reset_session(self):
        self.write(self.log, self.meta("old"), self.event("task_complete"))
        self.assertEqual(self.monitor.poll()[0].id, "old")
        self.write(self.log, self.meta("new"), mode="w")
        new, = self.monitor.poll()
        self.assertEqual((new.id, new.state, new.total_tokens), ("new", "idle", 0))
        # A rewrite larger than the old offset must also reset the reader.
        self.write(self.log, self.meta("third"), self.event("task_started"), mode="w")
        self.assertEqual(self.monitor.poll()[0].id, "third")

    def test_subagents_and_duplicate_fork_metadata(self):
        self.write(self.log, self.meta("fork"), self.meta("parent"), self.event("task_complete"))
        self.write(self.codex / "sub.jsonl", self.meta("sub", source={"subagent": "reviewer"}))
        self.write(self.claude / "sidechain.jsonl", {
            "type": "assistant", "isSidechain": True, "sessionId": "side", "timestamp": stamp(),
            "message": {"id": "side", "stop_reason": "end_turn"},
        })
        sessions = self.monitor.poll()
        self.assertEqual([s.id for s in sessions], ["fork"])

    def test_stale_activity_is_unknown_not_failure(self):
        meta = self.meta()
        meta["timestamp"] = stamp(-800)
        self.write(self.log, meta, self.event("task_started", -700))
        session, = self.monitor.poll()
        self.assertEqual(session.state, "unknown")
        self.assertIn("5 分钟", session.detail)
        self.write(self.log, self.event("turn_aborted"))
        self.assertEqual(self.monitor.poll()[0].state, "interrupted")

    def test_claude_stream_snapshots_are_deduplicated(self):
        path = self.claude / "session.jsonl"
        common = {"sessionId": "claude-test", "cwd": "/home/person/project", "entrypoint": "cli"}
        self.write(path, {**common, "type": "user", "timestamp": stamp(-10),
                          "message": {"content": "private prompt must not enter model"}})
        for output, stop in ((1, None), (5, "tool_use"), (10, "end_turn")):
            self.write(path, {**common, "type": "assistant", "timestamp": stamp(), "message": {
                "id": "message-1", "stop_reason": stop,
                "usage": {"input_tokens": 100, "output_tokens": output,
                          "cache_read_input_tokens": 30, "cache_creation_input_tokens": 20},
            }})
        session, = self.monitor.poll()
        self.assertEqual((session.state, session.source), ("completed", "CLI"))
        self.assertEqual((session.input_tokens, session.output_tokens, session.cached_tokens, session.total_tokens),
                         (150, 10, 30, 160))
        self.assertEqual(session.quota, [])
        self.assertIsNone(session.context_percent)
        self.assertNotIn("private prompt", repr(session))
        self.assertEqual(self.monitor.poll()[0].total_tokens, 160)
        self.write(path, {**common, "type": "assistant", "timestamp": stamp(), "message": {
            "id": "message-2", "stop_reason": "end_turn", "usage": {"input_tokens": 50, "output_tokens": 5}}})
        self.assertEqual(self.monitor.poll()[0].total_tokens, 215)

    def test_claude_error_and_interruption_are_visible(self):
        path = self.claude / "session.jsonl"
        self.write(path, {"type": "assistant", "sessionId": "test", "timestamp": stamp(-1),
                          "isApiErrorMessage": True, "message": {"content": "secret API response"}})
        failed, = self.monitor.poll()
        self.assertEqual(failed.state, "failed")
        self.assertNotIn("secret", failed.detail)
        self.write(path, {"type": "user", "sessionId": "test", "timestamp": stamp(), "message": {
            "content": [{"type": "text", "text": "[Request interrupted by user]"}]}})
        self.assertEqual(self.monitor.poll()[0].state, "interrupted")

    def test_missing_roots_and_environment_roots(self):
        self.assertEqual(Monitor(self.base / "missing-a", self.base / "missing-b").poll(), [])
        with patch.dict(os.environ, {"CODEX_HOME": str(self.codex.parent), "CLAUDE_CONFIG_DIR": str(self.claude.parent)}):
            monitor = Monitor()
            self.assertEqual(monitor.codex_root, self.codex)
            self.assertEqual(monitor.claude_root, self.claude)
        self.assertEqual(Monitor(self.codex.parent, self.claude.parent).codex_root, self.codex)

    def test_schema_noise_does_not_stop_other_records(self):
        self.write(self.log, self.meta(), {"type": "event_msg", "payload": {"type": []}},
                   self.event("token_count", info={"total_token_usage": {"input_tokens": "NaN", "output_tokens": -10}}),
                   self.event("task_complete"))
        self.write(self.claude / "noise.jsonl", {"type": []}, {"type": "assistant", "message": {"stop_reason": []}})
        sessions = self.monitor.poll()
        codex = next(session for session in sessions if session.provider == "codex")
        self.assertEqual((codex.state, codex.total_tokens), ("completed", 0))

    def test_only_latest_forty_files_are_retained(self):
        for i in range(45):
            path = self.codex / f"{i}.jsonl"
            self.write(path, self.meta(str(i)))
            os.utime(path, (time.time() - i, time.time() - i))
        sessions = self.monitor.poll()
        self.assertEqual(len(sessions), 40)
        self.assertNotIn("44", {session.id for session in sessions})

    def test_invalid_quota_reset_is_unknown(self):
        self.write(self.log, self.meta(), self.event("token_count", rate_limits={
            "primary": {"used_percent": 20, "resets_at": 1e300, "window_minutes": 300}}))
        self.assertIsNone(self.monitor.poll()[0].quota[0].resets_at)

    def test_recent_subagents_do_not_hide_main_sessions(self):
        self.write(self.log, self.meta("main"))
        os.utime(self.log, (time.time() - 100, time.time() - 100))
        for i in range(45):
            self.write(self.codex / f"sub-{i}.jsonl", self.meta(f"child-{i}", source={"subagent": "reviewer"}))
        self.assertEqual([session.id for session in self.monitor.poll()], ["main"])

    def test_claude_output_limit_does_not_signal_success(self):
        self.write(self.claude / "cutoff.jsonl", {"type": "assistant", "timestamp": stamp(),
                   "message": {"id": "cutoff", "stop_reason": "max_tokens"}})
        session, = self.monitor.poll()
        self.assertEqual(session.state, "interrupted")
        self.assertIn("截断", session.detail)


if __name__ == "__main__":
    unittest.main()
