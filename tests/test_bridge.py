"""The opt-in bridge is tested only in temporary directories."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from poketask.bridge import HOOK_EVENTS, apply_bridge, install_bridge, uninstall_bridge
from poketask.core import Session, QuotaWindow


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="poketask-bridge-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / "claude" / "settings.json"
        self.settings.parent.mkdir()
        self.data = self.root / "data space's"  # Command quoting must preserve this.
        self.bridge = self.data / "bridge"
        self.bridge.mkdir(parents=True)

    def config(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def snapshot(self, kind="hook", observed=None, **extra):
        payload = {"schema": 1, "provider": "claude", "session_id": "fixture",
                   "kind": kind, "observed_at": observed or time.time(), **extra}
        path = self.bridge / (hashlib.sha256(b"fixture").hexdigest() + f".{kind}.json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_install_is_idempotent_preserves_existing_status_and_hooks(self):
        original = {"theme": "dark", "statusLine": {"type": "command", "command": "my-status"},
                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-hook"}]}]}}
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        message = install_bridge(self.settings, self.data)
        self.assertIn("额度未增强", message)
        first = self.config()
        install_bridge(self.settings, self.data)
        self.assertEqual(self.config(), first)
        self.assertEqual(first["statusLine"], original["statusLine"])
        self.assertEqual(first["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        self.assertEqual(set(first["hooks"]), set(HOOK_EVENTS))
        self.assertTrue((self.data / "claude-bridge.ps1").is_file())
        self.assertEqual(len(list((self.data / "backups").glob("*.json"))), 1)

    def test_uninstall_preserves_edits_made_after_install(self):
        install_bridge(self.settings, self.data)
        current = self.config()
        current["model"] = "new-user-model"
        current["statusLine"] = {"type": "command", "command": "new-user-status"}
        current["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "new-user-hook"})
        self.settings.write_text(json.dumps(current), encoding="utf-8")
        uninstall_bridge(self.settings, self.data)
        after = self.config()
        self.assertEqual(after["model"], "new-user-model")
        self.assertEqual(after["statusLine"]["command"], "new-user-status")
        self.assertEqual(after["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "new-user-hook"}]}]})

    def test_install_uninstall_own_status_line(self):
        install_bridge(self.settings, self.data)
        self.assertIn("statusLine", self.config())
        uninstall_bridge(self.settings, self.data)
        self.assertEqual(self.config(), {})

    def test_invalid_settings_are_unchanged(self):
        self.settings.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            install_bridge(self.settings, self.data)
        self.assertEqual(self.settings.read_text(), "{invalid")

    def test_configuration_retries_when_another_writer_changes_it(self):
        from poketask import bridge
        self.settings.write_text('{"theme":"old"}', encoding="utf-8")
        actual = bridge._read_settings
        calls = 0
        def read(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.settings.write_text('{"theme":"concurrent","userSetting":42}', encoding="utf-8")
            return actual(path)
        with patch("poketask.bridge._read_settings", side_effect=read):
            install_bridge(self.settings, self.data)
        self.assertEqual(self.config()["theme"], "concurrent")
        self.assertEqual(self.config()["userSetting"], 42)

    def test_state_and_usage_have_independent_clocks(self):
        now = time.time()
        session = Session("fixture", "claude", "VS Code", "project", state="working", updated_at=now)
        self.snapshot("hook", now - 10, event="Stop")
        self.snapshot("statusline", now - 20, context_percent=32,
                      quota=[{"label": "5 小时", "used_percent": 19, "resets_at": now + 1000}])
        result, = apply_bridge([session], self.bridge)
        self.assertEqual(result.state, "working")
        self.assertEqual(result.context_percent, 32)
        self.assertEqual(result.quota[0].used_percent, 19)
        self.assertEqual(result.quota[0].observed_at, now - 20)
        self.assertEqual(session.quota, [], "The caller's input must not be mutated")
        self.snapshot("hook", now + 1, event="Stop")
        result, = apply_bridge([result], self.bridge)
        self.assertEqual(result.state, "completed")
        self.assertIn("本轮", result.detail)
        self.assertIn("不代表", result.detail)

    def test_tool_failure_recovers_and_idle_notification_is_not_waiting(self):
        now = time.time()
        self.snapshot(event="PostToolUseFailure", observed=now, is_interrupt=False)
        result, = apply_bridge([], self.bridge)
        self.assertEqual(result.state, "failed")
        self.snapshot(event="PreToolUse", observed=now + 1)
        result, = apply_bridge([result], self.bridge)
        self.assertEqual(result.state, "working")
        self.snapshot(event="Notification", notification_type="idle_prompt", observed=now + 2)
        result, = apply_bridge([result], self.bridge)
        self.assertEqual(result.state, "idle")
        self.snapshot(event="PermissionRequest", observed=now + 3)
        self.assertEqual(apply_bridge([result], self.bridge)[0].state, "asking")

    def test_bad_data_and_huge_reset_do_not_escape_parser(self):
        self.snapshot("statusline", context_percent=1e300,
                      quota=[{"label": "5 小时", "used_percent": 12, "resets_at": 1e300}])
        (self.bridge / "malformed.json").write_text("{broken", encoding="utf-8")
        result, = apply_bridge([], self.bridge)
        self.assertIsNone(result.context_percent)
        self.assertIsNone(result.quota[0].resets_at)
        self.snapshot("hook", observed=1e300, event="Stop")
        self.assertNotEqual(apply_bridge([], self.bridge)[0].state, "completed")

    def test_new_null_context_and_absent_quota_clear_old_snapshots(self):
        now = time.time()
        session = Session("fixture", "claude", "VS Code", "project", context_percent=50,
                          context_observed_at=now - 100,
                          quota=[QuotaWindow("5 小时", 30, now - 20, now - 100)])
        self.snapshot("statusline", now, context_percent=None, quota=[])
        result, = apply_bridge([session], self.bridge)
        self.assertIsNone(result.context_percent)
        self.assertEqual(result.quota, [])

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is required for the real bridge script")
    def test_real_powershell_script_whitelists_and_separates_payloads(self):
        install_bridge(self.settings, self.data)
        configuration = self.config()
        command = configuration["hooks"]["Stop"][0]["hooks"][0]["command"]
        # Only fixed flags and base64 are shell tokens. The embedded file paths
        # (including spaces and apostrophes) never pass through a shell parser.
        def invoke(command, payload):
            result = subprocess.run(command.split(), input=json.dumps(payload), text=True,
                                    encoding="utf-8", capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        common = {"session_id": "fixture", "cwd": "C:/project", "prompt": "SECRET PROMPT",
                  "tool_input": {"command": "SECRET TOOL"}, "last_assistant_message": "SECRET RESPONSE"}
        invoke(command, {**common, "hook_event_name": "PermissionRequest"})
        hook, = self.bridge.glob("*.hook.json")
        contents = hook.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", contents)
        self.assertEqual(set(json.loads(contents)), {"schema", "provider", "kind", "session_id", "observed_at", "cwd", "event"})
        output = invoke(configuration["statusLine"]["command"], {
            **common, "context_window": {"used_percentage": 17},
            "rate_limits": {"five_hour": {"used_percentage": 25, "resets_at": 1800000000}},
        })
        self.assertIn("PokeTask", output)
        self.assertIn("17%", output)
        self.assertEqual(len(list(self.bridge.glob("*.json"))), 2)
        state, = apply_bridge([], self.bridge)
        self.assertEqual((state.state, state.context_percent, state.quota[0].used_percent), ("asking", 17, 25))
        invoke(command, {**common, "hook_event_name": "Stop"})
        self.assertEqual(apply_bridge([], self.bridge)[0].state, "completed")
        self.assertEqual(list(self.bridge.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
