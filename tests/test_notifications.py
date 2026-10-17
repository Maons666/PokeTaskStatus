"""Regression for turns which both begin and end between polling intervals."""
import unittest
from unittest.mock import patch
from poketask.app import CompletionGate
from poketask.core import Session


class FastCompletionTests(unittest.TestCase):
    def test_fast_and_successive_turns_notify_once_each(self):
        with patch("poketask.app.time.time", return_value=1000):
            gate=CompletionGate()
        session=Session("s","codex","VS Code","fixture",state="idle",updated_at=1000)
        self.assertFalse(gate.update([session],"codex:s"))
        session.state="completed"
        session.updated_at=1001
        self.assertTrue(gate.update([session],"codex:s"))
        self.assertFalse(gate.update([session],"codex:s"))
        session.updated_at=1002
        self.assertTrue(gate.update([session],"codex:s"))
        self.assertFalse(gate.update([session],"codex:s"))

    def test_partial_history_finishing_catchup_is_silent(self):
        with patch("poketask.app.time.time",return_value=1000):
            gate=CompletionGate()
        session=Session("s","codex","VS Code","fixture",state="working",updated_at=990,usage_partial=True)
        gate.update([session],"codex:s")
        session.state="completed"
        session.updated_at=999
        gate.update([session],"codex:s")
        session.usage_partial=False
        self.assertFalse(gate.update([session],"codex:s"))
