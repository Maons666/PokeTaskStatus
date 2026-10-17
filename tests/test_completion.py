"""Notification behavior checks; no GUI, audio, or real session data."""

import unittest
from unittest.mock import patch

from poketask.app import CompletionGate
from poketask.core import Session


class CompletionTests(unittest.TestCase):
    def setUp(self):
        with patch("poketask.app.time.time", return_value=1000):
            self.gate = CompletionGate()

    @staticmethod
    def session(state, identity="selected", stamp=1001, partial=False):
        return Session(identity, "codex", "VS Code", "fixture", state=state,
                       updated_at=stamp, usage_partial=partial)

    def test_startup_completed_history_does_not_notify(self):
        self.assertFalse(self.gate.update([self.session("completed")], "codex:selected"))

    def test_selected_completion_notifies_once(self):
        self.assertFalse(self.gate.update([self.session("working")], "codex:selected"))
        self.assertTrue(self.gate.update([self.session("completed")], "codex:selected"))
        self.assertFalse(self.gate.update([self.session("completed")], "codex:selected"))

    def test_waiting_completion_notifies(self):
        self.gate.update([self.session("waiting")], "codex:selected")
        self.assertTrue(self.gate.update([self.session("completed")], "codex:selected"))

    def test_unselected_completion_and_later_switch_are_silent(self):
        self.gate.update([self.session("working", "other")], "codex:selected")
        self.assertFalse(self.gate.update([self.session("completed", "other")], "codex:selected"))
        self.assertFalse(self.gate.update([self.session("completed", "other")], "codex:other"))

    def test_failure_and_interruption_do_not_play_success_sound(self):
        for state in ("failed", "interrupted", "unknown"):
            with self.subTest(state=state):
                self.gate.update([self.session("working")], "codex:selected")
                self.assertFalse(self.gate.update([self.session(state)], "codex:selected"))

    def test_prelaunch_completion_is_silent(self):
        self.gate.update([self.session("working", stamp=900)], "codex:selected")
        self.assertFalse(self.gate.update([self.session("completed", stamp=999)], "codex:selected"))

    def test_partial_log_history_is_silent(self):
        self.gate.update([self.session("working")], "codex:selected")
        self.assertFalse(self.gate.update([self.session("completed", partial=True)], "codex:selected"))


if __name__ == "__main__":
    unittest.main()
