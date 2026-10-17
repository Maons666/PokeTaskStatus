"""Explicit user-input state; ordinary tool work and prose are not questions."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from poketask.bridge import apply_bridge, install_bridge
from poketask.core import STATES
from poketask.monitor import Monitor


class AskingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="poketask-asking-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex, self.claude, self.bridge = [self.root / part for part in ("sessions", "projects", "bridge")]
        for path in (self.codex, self.claude, self.bridge):
            path.mkdir()
        self.log = self.codex / "fixture.jsonl"
        self.monitor = Monitor(self.codex, self.claude, self.bridge)
        self.started = time.time()
        self.counter = 0
        self.append("session_meta", {"id": "fixture", "source": "vscode"})
        self.event("task_started")

    def append(self, kind, payload):
        self.counter += 1
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": kind, "timestamp": self.started + self.counter / 1000,
                                     "payload": payload}) + "\n")

    def event(self, event, **extra):
        self.append("event_msg", {"type": event, **extra})

    def response(self, kind, **extra):
        self.append("response_item", {"type": kind, **extra})

    def state(self):
        session, = self.monitor.poll()
        return session.state

    def hook(self, event, **extra):
        self.counter += 1
        payload = {"schema": 1, "provider": "claude", "kind": "hook", "session_id": "claude-fixture",
                   "observed_at": self.started + self.counter / 1000, "event": event, **extra}
        path = self.bridge / (hashlib.sha256(b"claude-fixture").hexdigest() + ".hook.json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return apply_bridge([], self.bridge)[0].state

    def test_asking_is_distinct_and_waiting_remains_compatible(self):
        self.assertIn("asking", STATES)
        self.assertIn("waiting", STATES)
        for event in ("approval_requested", "request_user_input"):
            with self.subTest(event=event):
                self.event(event)
                self.assertEqual(self.state(), "asking")
                self.event("task_started")
                self.assertEqual(self.state(), "working")

    def test_question_function_and_custom_call_resolve_by_matching_id(self):
        for kind, output in (("function_call", "function_call_output"), ("custom_tool_call", "custom_tool_call_output")):
            with self.subTest(kind=kind):
                self.response(kind, name="request_user_input", call_id=kind, arguments="PRIVATE INPUT")
                self.assertEqual(self.state(), "asking")
                self.response(output, call_id="another-call", output="unrelated result")
                self.assertEqual(self.state(), "asking")
                self.response(output, call_id=kind, output="PRIVATE ANSWER")
                self.assertEqual(self.state(), "working")
                self.assertNotIn("PRIVATE", repr(self.monitor.poll()))

    def test_multiple_pending_questions_require_all_matching_returns(self):
        for call in ("first", "second"):
            self.response("function_call", name="request_user_input", call_id=call)
        self.assertEqual(self.state(), "asking")
        self.response("function_call_output", call_id="first")
        self.assertEqual(self.state(), "asking")
        self.response("function_call_output", call_id="second")
        self.assertEqual(self.state(), "working")

    def test_plain_questions_and_ordinary_tools_do_not_become_asking(self):
        for payload in ({"type": "message", "role": "assistant", "content": "Do you want to continue?"},
                        {"type": "function_call", "name": "read_file", "call_id": "regular", "arguments": "request_user_input"},
                        {"type": "custom_tool_call", "name": "some.request_user_input", "call_id": "custom"},
                        {"type": "function_call", "name": "request_user_input", "call_id": "partial", "status": "in_progress"},
                        {"type": "function_call", "name": "request_user_input"}):
            self.append("response_item", payload)
            self.assertEqual(self.state(), "working")

    def test_usage_updates_do_not_cancel_question_and_new_user_input_resumes(self):
        self.response("function_call", name="functions.request_user_input", call_id="question")
        self.event("token_count", rate_limits={"primary": {"used_percent": 10}})
        self.assertEqual(self.state(), "asking")
        self.response("message", role="user", content="synthetic input")
        self.assertEqual(self.state(), "working")

    def test_question_can_finish_or_abort_without_late_output_reopening_turn(self):
        for event, expected in (("task_complete", "completed"), ("turn_aborted", "interrupted"), ("task_failed", "failed")):
            with self.subTest(event=event):
                self.event("task_started")
                self.response("function_call", name="request_user_input", call_id="question")
                self.assertEqual(self.state(), "asking")
                self.event(event)
                self.response("function_call_output", call_id="question")
                self.assertEqual(self.state(), expected)

    def test_claude_explicit_questions_idle_and_resume_are_separate(self):
        self.assertEqual(self.hook("PermissionRequest"), "asking")
        for kind in ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            self.assertEqual(self.hook("Notification", notification_type=kind), "asking")
        self.assertEqual(self.hook("Notification", notification_type="idle_prompt"), "idle")
        for kind in ("elicitation_complete", "elicitation_response"):
            self.assertEqual(self.hook("Notification", notification_type=kind), "working")
        for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            self.assertEqual(self.hook(event), "working")
        self.assertEqual(self.hook("Stop"), "completed")
        self.assertEqual(self.hook("PostToolUseFailure", is_interrupt=True), "interrupted")

    @unittest.skipUnless(shutil.which("powershell.exe"), "Requires Windows PowerShell")
    def test_real_bridge_preserves_asking_and_resolution_notifications(self):
        settings = self.root / "settings.json"
        install_bridge(settings, self.root)
        configuration = json.loads(settings.read_text(encoding="utf-8"))
        command = configuration["hooks"]["Notification"][0]["hooks"][0]["command"]
        for notification, expected in (("agent_needs_input", "asking"), ("elicitation_response", "working"), ("idle_prompt", "idle")):
            payload = {"session_id": "claude-fixture", "hook_event_name": "Notification",
                       "notification_type": notification, "message": "PRIVATE QUESTION"}
            process = subprocess.run(command.split(), input=json.dumps(payload), text=True, encoding="utf-8",
                                     capture_output=True, timeout=15,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(process.returncode, 0)
            session, = apply_bridge([], self.bridge)
            self.assertEqual(session.state, expected)
            self.assertNotIn("PRIVATE QUESTION", repr(session))


if __name__ == "__main__":
    unittest.main()
