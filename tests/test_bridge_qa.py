"""Independent QA: isolated CLI execution, identity, stale data, and regressions."""
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
from poketask.bridge import apply_bridge, install_bridge, uninstall_bridge
from poketask.core import Session
from poketask.monitor import Monitor


class BridgeQATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="poketask-independent-qa-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "测试 data's"
        self.bridge = self.data / "bridge"
        self.bridge.mkdir(parents=True)
        self.settings = self.root / "isolated-claude" / "settings.json"
        self.claude = self.root / "projects"
        self.codex = self.root / "sessions"
        self.claude.mkdir()
        self.codex.mkdir()

    def snapshot(self, identity="fixture", kind="hook", observed=None, **extra):
        data = {"schema": 1, "provider": "claude", "session_id": identity,
                "kind": kind, "observed_at": time.time() if observed is None else observed, **extra}
        filename = hashlib.sha256(identity.encode("utf-8")).hexdigest() + f".{kind}.json"
        path = self.bridge / filename
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_same_identity_different_provider_never_shares_quota(self):
        self.snapshot(kind="statusline", quota=[{"label": "5 小时", "used_percent": 44}])
        codex = Session("fixture", "codex", "VS Code", "Codex")
        claude = Session("fixture", "claude", "VS Code", "Claude")
        results = {s.provider: s for s in apply_bridge([codex, claude], self.bridge)}
        self.assertEqual(results["codex"].quota, [])
        self.assertEqual(results["claude"].quota[0].used_percent, 44)
        self.assertEqual(claude.quota, [])

    def test_expired_orphan_and_stale_active_event_are_not_live(self):
        self.snapshot(observed=time.time() - 90000, event="Stop")
        self.assertEqual(apply_bridge([], self.bridge), [])
        self.snapshot(observed=time.time() - 600, event="PreToolUse")
        session, = apply_bridge([], self.bridge)
        self.assertEqual(session.state, "unknown")

    def test_expired_quota_preserves_observation_time_for_stale_label(self):
        observed = time.time() - 1800
        reset = time.time() - 100
        self.snapshot(kind="statusline", observed=observed,
                      quota=[{"label": "5 小时", "used_percent": 80, "resets_at": reset}])
        session, = apply_bridge([], self.bridge)
        self.assertEqual(session.quota[0].observed_at, observed)
        self.assertEqual(session.quota[0].resets_at, reset)

    def test_invalid_bridge_percent_is_unknown(self):
        for value in ("NaN", True, -1, 101, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.snapshot(kind="statusline", quota=[{"label": "5 小时", "used_percent": value}])
                session, = apply_bridge([], self.bridge)
                self.assertEqual(session.quota, [])

    def test_codex_invalid_quota_does_not_claim_full_remaining(self):
        path = self.codex / "fixture.jsonl"
        for value in ("NaN", None, True, -1, 101):
            with self.subTest(value=value):
                rows = [{"type": "session_meta", "payload": {"id": "fixture", "source": "vscode"}},
                        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                            "primary": {"used_percent": value, "window_minutes": 300}}}}]
                path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                session, = Monitor(self.codex, self.claude).poll()
                self.assertEqual(session.quota, [], "Malformed quota must remain unknown")

    def test_claude_log_and_stop_hook_notify_only_once_per_turn(self):
        now = time.time()
        path = self.claude / "fixture.jsonl"
        def append(kind, timestamp, **extra):
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"sessionId": "fixture", "type": kind,
                                         "timestamp": timestamp, **extra}) + "\n")
        with patch("poketask.app.time.time", return_value=now):
            gate = CompletionGate()
        monitor = Monitor(self.codex, self.claude, self.bridge)
        append("user", now, message={"content": "synthetic"})
        self.assertFalse(gate.update(monitor.poll(), "claude:fixture"))
        append("assistant", now + .01, message={"id": "answer", "stop_reason": "end_turn"})
        self.assertTrue(gate.update(monitor.poll(), "claude:fixture"))
        self.snapshot(observed=now + .02, event="Stop")
        self.assertFalse(gate.update(monitor.poll(), "claude:fixture"),
                         "The Stop hook confirms the same response already observed in the log")

    @unittest.skipUnless(shutil.which("powershell.exe"), "Requires Windows PowerShell")
    def test_real_statusline_unicode_and_invalid_inputs_then_uninstall(self):
        install_bridge(self.settings, self.data)
        config = json.loads(self.settings.read_text(encoding="utf-8"))
        command = config["statusLine"]["command"]
        def invoke(payload):
            result = subprocess.run(command.split(), input=json.dumps(payload, ensure_ascii=False),
                                    encoding="utf-8", text=True, capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        payload = {"session_id": "中文 ' 会话", "cwd": "C:/测试 project", "prompt": "DO NOT PERSIST",
                   "context_window": {"used_percentage": 39}, "rate_limits": {
                       "five_hour": {"used_percentage": 42, "resets_at": time.time() + 3000},
                       "seven_day": {"used_percentage": "NaN"}}}
        self.assertIn("39%", invoke(payload))
        session, = apply_bridge([], self.bridge)
        self.assertEqual((session.id, session.context_percent), ("中文 ' 会话", 39))
        self.assertEqual(len(session.quota), 1)
        files = list(self.bridge.glob("*.json"))
        self.assertEqual(len(files), 1)
        original = files[0].read_bytes()
        self.assertNotIn(b"DO NOT PERSIST", original)
        self.assertEqual(invoke({**payload, "agent_id": "child-agent"}), "")
        self.assertEqual(invoke({"session_id": []}), "")
        self.assertEqual(files[0].read_bytes(), original)
        uninstall_bridge(self.settings, self.data)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), {})
        self.assertEqual(list(self.bridge.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
