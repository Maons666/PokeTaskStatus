"""Cross-source completion identity and fast-turn regressions."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from poketask.app import CompletionGate
from poketask.bridge import _finite, apply_bridge, install_bridge, uninstall_bridge
from poketask.monitor import Monitor


class TurnTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="poketask-turns-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.claude, self.codex, self.bridge = [self.root / name for name in ("projects", "sessions", "bridge")]
        for directory in (self.claude, self.codex, self.bridge):
            directory.mkdir()
        self.now = time.time()
        self.monitor = Monitor(self.codex, self.claude, self.bridge)
        with patch("poketask.app.time.time", return_value=self.now - 1):
            self.gate = CompletionGate()

    def log(self, kind, offset, **extra):
        with (self.claude / "fixture.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": kind, "sessionId": "fixture", "timestamp": self.now + offset, **extra}) + "\n")

    def prompt(self, name, offset):
        self.log("user", offset, uuid=name, message={"content": "fixture prompt"})

    def finish(self, name, offset):
        self.log("assistant", offset, message={"id": name, "stop_reason": "end_turn"})

    def hook(self, event, offset, **extra):
        name = hashlib.sha256(b"fixture").hexdigest() + ".hook.json"
        data = {"schema": 1, "provider": "claude", "session_id": "fixture", "kind": "hook",
                "event": event, "observed_at": self.now + offset, **extra}
        (self.bridge / name).write_text(json.dumps(data), encoding="utf-8")

    def poll(self):
        return self.gate.update(self.monitor.poll(), "claude:fixture")

    def test_stop_before_transcript_flush_notifies_once(self):
        self.prompt("user-1", 0)
        self.assertFalse(self.poll())
        self.hook("Stop", .02)
        self.assertTrue(self.poll())
        # Async transcript flush can arrive later, even with a later timestamp.
        self.finish("answer-1", .03)
        self.assertFalse(self.poll())
        self.assertFalse(self.poll())

    def test_new_fast_turn_with_both_sources_is_not_lost(self):
        self.prompt("user-1", 0)
        self.assertFalse(self.poll())
        self.finish("answer-1", .01)
        self.assertTrue(self.poll())
        self.hook("Stop", .02, turn_id="claude-hook:one", turn_started_at=self.now,
                  completion_id="claude-hook:one", completed_at=self.now + .02,
                  completion_turn_started_at=self.now)
        self.assertFalse(self.poll())
        # Both start and completion occur before the next poll, without cooldown.
        self.prompt("user-2", .021)
        self.finish("answer-2", .022)
        self.hook("Stop", .023, turn_id="claude-hook:two", turn_started_at=self.now + .021,
                  completion_id="claude-hook:two", completed_at=self.now + .023,
                  completion_turn_started_at=self.now + .021)
        self.assertTrue(self.poll())
        self.assertFalse(self.poll())
        self.assertEqual(self.monitor.poll()[0].state, "completed")

    def test_bridge_only_turns_use_retained_prompt_identity(self):
        self.hook("SessionStart", 0)
        self.assertFalse(self.poll())
        for index in range(1, 4):
            moment = index / 1000
            identity = f"prompt:{index}"
            self.hook("Stop", moment + .0001, turn_id=identity,
                      turn_started_at=self.now + moment, completion_id=identity,
                      completed_at=self.now + moment + .0001,
                      completion_turn_started_at=self.now + moment)
            self.assertTrue(self.poll())
            self.assertFalse(self.poll())

    def test_completed_event_survives_following_working_state(self):
        self.prompt("user-1", 0)
        self.assertFalse(self.poll())
        self.finish("answer-1", .01)
        self.prompt("user-2", .02)
        self.assertEqual(self.monitor.poll()[0].state, "working")
        self.assertTrue(self.poll(), "The completed first turn must not disappear behind the next task")
        self.assertFalse(self.poll())

    def test_tool_result_does_not_create_a_new_user_turn(self):
        self.prompt("user-1", 0)
        self.poll()
        before = self.monitor.poll()[0].turn_id
        self.log("user", .01, message={"content": [{"type": "tool_result", "tool_use_id": "tool"}]})
        self.assertEqual(self.monitor.poll()[0].turn_id, before)
        self.finish("answer-1", .02)
        self.assertTrue(self.poll())
        self.finish("answer-1", .03)
        self.assertFalse(self.poll(), "Repeated streaming end_turn snapshots are one completion")

    def test_missing_event_time_does_not_use_recent_file_mtime_for_sound(self):
        self.prompt("user", 0)
        self.poll()
        with (self.claude / "fixture.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "assistant", "sessionId": "fixture",
                                    "message": {"id": "old", "stop_reason": "end_turn"}}) + "\n")
        self.assertFalse(self.poll())

    def test_invalid_new_quota_preserves_old_observation_time(self):
        path = self.codex / "fixture.jsonl"
        rows = [{"type": "session_meta", "timestamp": self.now, "payload": {"id": "fixture"}},
                {"type": "event_msg", "timestamp": self.now, "payload": {"type": "token_count",
                    "rate_limits": {"primary": {"used_percent": 30, "window_minutes": 300}}}},
                {"type": "event_msg", "timestamp": self.now + 2, "payload": {"type": "token_count",
                    "rate_limits": {"primary": {"used_percent": None, "window_minutes": 300}}}}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        session, = self.monitor.poll()
        self.assertEqual(session.quota[0].used_percent, 30)
        self.assertEqual(session.quota[0].observed_at, self.now)
        self.assertIsNone(_finite(10 ** 1000))

    def test_uninstall_hides_archived_observations_and_reinstall_starts_fresh(self):
        settings = self.root / "settings.json"
        install_bridge(settings, self.root)
        self.hook("PreToolUse", .01)
        self.assertEqual(apply_bridge([], self.bridge)[0].state, "working")
        uninstall_bridge(settings, self.root)
        self.assertEqual(apply_bridge([], self.bridge), [])
        self.assertTrue(list(self.bridge.glob("*.json")), "Archived data remains local for inspection")
        with patch("poketask.bridge.time.time", return_value=self.now + 1):
            install_bridge(settings, self.root)
            self.assertEqual(apply_bridge([], self.bridge), [])

    @unittest.skipUnless(shutil.which("powershell.exe"), "Requires Windows PowerShell")
    def test_real_script_retains_turn_across_stop_and_following_prompt(self):
        settings = self.root / "settings.json"
        install_bridge(settings, self.root)
        config = json.loads(settings.read_text(encoding="utf-8"))
        command = config["hooks"]["Stop"][0]["hooks"][0]["command"]
        def invoke(event, prompt=None):
            payload = {"session_id": "fixture", "hook_event_name": event, "prompt": "PRIVATE DO NOT SAVE"}
            if prompt:
                payload["prompt_id"] = prompt
            process = subprocess.run(command.split(), input=json.dumps(payload), encoding="utf-8",
                                     text=True, capture_output=True, timeout=15,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(process.returncode, 0)
            path, = self.bridge.glob("*.hook.json")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE", text)
            return json.loads(text)
        first = invoke("UserPromptSubmit", "one")
        self.assertEqual(first["turn_id"], "prompt:one")
        stopped = invoke("Stop", "one")
        self.assertEqual(stopped["completion_id"], "prompt:one")
        duplicate = invoke("Stop", "one")
        self.assertEqual(duplicate["completed_at"], stopped["completed_at"])
        second = invoke("UserPromptSubmit", "two")
        self.assertEqual(second["turn_id"], "prompt:two")
        self.assertEqual(second["completion_id"], "prompt:one")
        self.assertEqual(second["completed_at"], stopped["completed_at"])
        completed = invoke("Stop", "two")
        self.assertEqual(completed["completion_id"], "prompt:two")
        self.assertGreater(completed["completed_at"], stopped["completed_at"])


if __name__ == "__main__":
    unittest.main()
