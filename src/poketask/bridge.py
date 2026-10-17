"""Opt-in Claude Code bridge installation and whitelisted observation reader.

Importing this module never modifies settings. Installation is an explicit UI
action. Uninstall edits the current configuration and only removes our exact
commands; it never restores an old settings backup over unrelated changes.
"""

from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Callable
import uuid

from .core import Session, QuotaWindow
from .paths import assets_dir


HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "PermissionRequest", "Notification", "Stop",
    "StopFailure", "SessionEnd",
)
_NOTIFICATIONS = {"permission_prompt", "idle_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}
_RESUMED_NOTIFICATIONS = {"elicitation_complete", "elicitation_response"}
_MAX_SNAPSHOT = 32 * 1024


def _bridge_command(data_root: Path, mode: str) -> str:
    def literal(value: Path | str) -> str:
        return "'" + str(value).replace("'", "''") + "'"
    script = data_root.resolve() / "claude-bridge.ps1"
    invocation = f"& {literal(script)} -Mode {literal(mode)} -DataDir {literal(data_root.resolve())}"
    encoded = base64.b64encode(invocation.encode("utf-16le")).decode("ascii")
    return ("powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden "
            "-ExecutionPolicy Bypass -EncodedCommand " + encoded)


def _read_settings(path: Path) -> tuple[bytes | None, dict]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Claude 配置不是有效 JSON，未修改配置") from exc
    if not isinstance(data, dict):
        raise ValueError("Claude 配置必须是 JSON 对象，未修改配置")
    return raw, data


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".poketask-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def _update_settings(settings_path: Path, data_root: Path,
                     transform: Callable[[dict], str]) -> str:
    """Re-read before replacement, retrying if another writer changed settings."""
    settings_path, data_root = Path(settings_path), Path(data_root)
    for _ in range(5):
        original, settings = _read_settings(settings_path)
        result = transform(settings)
        encoded = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        latest, _ = _read_settings(settings_path)
        if latest != original:
            continue
        if original is not None and json.loads(original.decode("utf-8-sig")) == settings:
            return result
        if original is not None:
            backup = data_root / "backups" / ("claude-settings-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".json")
            _atomic_bytes(backup, original)
        # A backup operation can take time; check again before the final write.
        latest, _ = _read_settings(settings_path)
        if latest != original:
            continue
        _atomic_bytes(settings_path, encoded)
        return result
    raise RuntimeError("Claude 配置正在被其他程序修改；请稍后重试")


def _remove_owned(settings: dict, commands: set[str]) -> None:
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            retained = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    retained.append(group)
                    continue
                remaining = [handler for handler in group["hooks"] if not (
                    isinstance(handler, dict) and handler.get("type") == "command"
                    and isinstance(handler.get("command"), str) and handler.get("command") in commands
                )]
                if len(remaining) == len(group["hooks"]):
                    retained.append(group)
                elif remaining:
                    retained.append({**group, "hooks": remaining})
            if retained:
                hooks[event] = retained
            elif groups:
                del hooks[event]
        if not hooks:
            settings.pop("hooks", None)
    status = settings.get("statusLine")
    if isinstance(status, dict) and status.get("type") == "command" and isinstance(status.get("command"), str) and status.get("command") in commands:
        settings.pop("statusLine", None)


def install_bridge(settings_path: Path, data_root: Path) -> str:
    """Install only when the user presses the UI's explicit enable button."""
    data_root = Path(data_root).resolve()
    # Validate before creating/copying the executable script or settings backup.
    _read_settings(Path(settings_path))
    source = assets_dir().parent / "claude-bridge.ps1"
    _atomic_bytes(data_root / "claude-bridge.ps1", source.read_bytes())
    hook = _bridge_command(data_root, "hook")
    status = _bridge_command(data_root, "statusline")

    def transform(settings: dict) -> str:
        hooks = settings.get("hooks", {})
        if not isinstance(hooks, dict) or any(
            event in hooks and not isinstance(hooks[event], list) for event in HOOK_EVENTS
        ):
            raise ValueError("现有 hooks 配置格式不兼容，未修改配置")
        _remove_owned(settings, {hook, status})
        hooks = settings.setdefault("hooks", {})
        for event in HOOK_EVENTS:
            hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": hook, "timeout": 5}]})
        if "statusLine" not in settings:
            settings["statusLine"] = {"type": "command", "command": status}
            return "Claude 状态增强已安装；支持的会话将同步额度与上下文。请重启 Claude 会话。"
        return "Claude 状态增强已安装；已保留原 statusLine，额度未增强。请重启 Claude 会话。"

    result = _update_settings(Path(settings_path), data_root, transform)
    # An old running Claude process may still have cached hook commands. The
    # marker lets those commands exit quietly and hides archived observations.
    disabled = data_root / "bridge" / ".disabled"
    was_disabled = disabled.exists()
    disabled.unlink(missing_ok=True)
    enabled = data_root / "bridge" / ".enabled-at"
    if was_disabled or not enabled.exists():
        _atomic_bytes(enabled, str(time.time()).encode("ascii"))
    return result


def uninstall_bridge(settings_path: Path, data_root: Path) -> str:
    """Remove only this installation's commands, preserving later user edits."""
    data_root = Path(data_root).resolve()
    commands = {_bridge_command(data_root, mode) for mode in ("hook", "statusline")}
    if not Path(settings_path).exists():
        _atomic_bytes(data_root / "bridge" / ".disabled", b"disabled\n")
        return "没有找到 Claude 配置；无需移除。"

    def transform(settings: dict) -> str:
        _remove_owned(settings, commands)
        return "Claude 增强已移除；其他配置与备份已保留。请重启 Claude 会话。"

    result = _update_settings(Path(settings_path), data_root, transform)
    _atomic_bytes(data_root / "bridge" / ".disabled", b"disabled\n")
    return result


def _finite(value, minimum=0, maximum=100) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and minimum <= number <= maximum else None


def _bridge_state(data: dict) -> tuple[str | None, str]:
    event = data.get("event")
    if event in ("PreToolUse", "PostToolUse", "UserPromptSubmit"):
        return "working", "Claude 本地事件"
    if event == "SessionStart":
        return "idle", "Claude 会话已启动"
    if event == "Stop":
        return "completed", "本轮响应结束（不代表整个项目已完成）"
    if event == "StopFailure":
        return "failed", "Claude 本轮响应因 API 错误结束"
    if event == "PostToolUseFailure":
        if data.get("is_interrupt") is True:
            return "interrupted", "工具调用中断"
        return "failed", "一次工具调用失败；后续活动可恢复工作状态"
    if event == "PermissionRequest":
        return "asking", "需要用户授权工具调用"
    if event == "SessionEnd":
        return "disconnected", "Claude 会话已结束"
    if event == "Notification":
        notification = data.get("notification_type")
        if isinstance(notification, str) and notification in _NOTIFICATIONS - {"idle_prompt"}:
            return "asking", "需要用户授权、选择或输入"
        if isinstance(notification, str) and notification in _RESUMED_NOTIFICATIONS:
            return "working", "用户输入请求已返回，继续工作"
        if notification == "idle_prompt":
            return "idle", "Claude 当前空闲"
    return None, ""


def apply_bridge(sessions: list[Session], root: Path) -> list[Session]:
    """Merge schema-v1 snapshots without allowing stale hooks to rewind logs."""
    result = {(s.provider, s.id): replace(s, quota=[replace(q) for q in s.quota]) for s in sessions}
    now = time.time()
    root = Path(root)
    if (root / ".disabled").exists():
        return list(result.values())
    try:
        enabled_at = _finite(float((root / ".enabled-at").read_text(encoding="ascii")), 0, now + 60) or 0
    except (OSError, ValueError, UnicodeError):
        enabled_at = 0
    try:
        files = sorted(Path(root).glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:160]
    except OSError:
        return list(result.values())
    for path in files:
        try:
            if path.stat().st_size > _MAX_SNAPSHOT:
                continue
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(data, dict) or data.get("schema") != 1 or data.get("provider") != "claude":
            continue
        identity = data.get("session_id")
        kind = data.get("kind")
        if not isinstance(identity, str) or not 1 <= len(identity) <= 256 or kind not in ("hook", "statusline"):
            continue
        expected = hashlib.sha256(identity.encode("utf-8")).hexdigest() + "." + kind + ".json"
        if path.name != expected:
            continue
        observed = _finite(data.get("observed_at"), 1, min(now + 60, 32_503_680_000))
        if observed is None or observed < enabled_at:
            continue
        key = ("claude", identity)
        session = result.get(key)
        if session is None:
            # Do not make an old, orphaned hook look like a newly active session.
            if now - observed > 86400:
                continue
            cwd = data.get("cwd")
            cwd = cwd if isinstance(cwd, str) and len(cwd) <= 4096 else ""
            name = cwd.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or "Claude Code"
            session = Session(identity, "claude", "Unknown", name, cwd=cwd, updated_at=0)
            result[key] = session
        if kind == "hook":
            _merge_hook_completion(session, data, observed)
            state, detail = _bridge_state(data)
            if state is not None and observed >= session.updated_at:
                session.state, session.detail, session.updated_at = state, detail, observed
                if state == "working" and now - observed > 300:
                    session.state, session.detail = "unknown", "事件已超过 5 分钟未更新，无法确认当前工作状态"
        else:
            context = _finite(data.get("context_percent"))
            if "context_percent" in data and observed >= session.context_observed_at:
                session.context_percent, session.context_observed_at = context, observed
            quota = data.get("quota")
            if isinstance(quota, list) and observed >= max((q.observed_at for q in session.quota), default=0):
                parsed = []
                for window in quota[:2]:
                    if not isinstance(window, dict) or window.get("label") not in ("5 小时", "7 天"):
                        continue
                    percent = _finite(window.get("used_percent"))
                    if percent is not None:
                        reset = _finite(window.get("resets_at"), 1, 32_503_680_000)
                        parsed.append(QuotaWindow(window["label"], percent, reset, observed))
                session.quota = parsed
    for session in result.values():
        if session.updated_at == 0:
            session.updated_at = max(session.context_observed_at, max((q.observed_at for q in session.quota), default=0)) or now
            session.detail = "仅收到用量快照，尚无状态事件"
    return list(result.values())


def _identity(value) -> str:
    return value if isinstance(value, str) and 0 < len(value) <= 512 else ""


def _merge_hook_completion(session: Session, data: dict, observed: float) -> None:
    """Keep the last successful turn across later working/asking snapshots."""
    turn_id = _identity(data.get("turn_id"))
    started = _finite(data.get("turn_started_at"), 0, observed) or 0
    if turn_id and (not session.turn_id or started > max(session.turn_started_at, session.completed_at)):
        session.turn_id, session.turn_started_at = turn_id, started
    completion = _identity(data.get("completion_id"))
    completed = _finite(data.get("completed_at"), 0, observed) or 0
    completion_start = _finite(data.get("completion_turn_started_at"), 0, observed) or 0
    if data.get("event") == "Stop" and not completion:
        completion = turn_id or session.turn_id or "claude-hook:unidentified"
        completed = observed
        completion_start = started or session.turn_started_at
    if not completion or completed < session.completed_at:
        return
    if session.completion_id:
        same_identity = completion == session.completion_id
        if same_identity:
            return
    session.completion_id = completion
    session.completed_at = completed
    session.completion_turn_started_at = completion_start
