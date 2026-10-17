"""Read local Codex and Claude Code JSONL observations, without network access.

Only status, timestamps, project paths, usage and published rate-limit windows
leave the parser. Prompts, assistant prose, tool arguments and credentials are
never exposed. A session log is evidence of activity, not proof that its process
is still running; stale active turns consequently become ``unknown``.

Explicit roots may be configuration directories or their sessions/projects
directories. Defaults respect CODEX_HOME and CLAUDE_CONFIG_DIR.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from .core import QuotaWindow, Session


_READ_CHUNK = 256 * 1024
_FILE_READ_BUDGET = 2 * 1024 * 1024
_MAX_LINE = 16 * 1024 * 1024
_STALE_SECONDS = 300
_MAX_SESSIONS = 40


def _number(value: Any, default: float = 0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _count(value: Any) -> int:
    return max(0, int(_number(value)))


def _percentage(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = _number(value, -1)
    return number if 0 <= number <= 100 else None


def _timestamp(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = _number(value, fallback)
        result = result / 1000 if result > 100_000_000_000 else result
        return result if 0 <= result <= 32_503_680_000 else fallback
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            return result if 0 <= result <= 32_503_680_000 else fallback
        except (ValueError, OverflowError, OSError):
            pass
    return fallback


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _project_name(cwd: str, fallback: str) -> str:
    # A Windows log may be inspected on a Unix machine, or conversely.
    clean = cwd.rstrip("/\\").replace("\\", "/")
    return clean.rsplit("/", 1)[-1] if clean else fallback


def _source(value: Any, originator: Any = None) -> str:
    values = " ".join(str(v).lower() for v in (value, originator) if isinstance(v, str))
    if "vscode" in values or "vs code" in values:
        return "VS Code"
    if "desktop" in values or "app-server" in values or "app_server" in values:
        return "Desktop"
    if any(v in values for v in ("cli", "terminal", "exec")):
        return "CLI"
    return "Unknown"


@dataclass
class _Record:
    path: Path
    provider: str
    offset: int = 0
    pending: bytes = b""
    identity: tuple[int, int] | None = None
    checkpoint: bytes = b""
    session: Session | None = None
    excluded: bool = False
    meta_seen: bool = False
    overlong: bool = False
    malformed: int = 0
    # Claude emits multiple snapshots for the same assistant message id.
    messages: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    eof: bool = True
    line_count: int = 0
    asking_calls: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.offset = 0
        self.pending = b""
        self.checkpoint = b""
        self.session = None
        self.excluded = False
        self.meta_seen = False
        self.overlong = False
        self.malformed = 0
        self.messages.clear()
        self.line_count = 0
        self.asking_calls.clear()


class Monitor:
    """Stateful, incremental reader. Call ``poll`` from a single worker thread.

    ``errors`` contains only safe summaries, never log line contents. Returned
    sessions are detached snapshots and may safely be handed to the UI thread.
    Missing roots simply produce an empty list; no login is attempted.
    """

    def __init__(self, codex_root: Path | None = None, claude_root: Path | None = None,
                 bridge_root: Path | None = None):
        default_codex = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        default_claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        self.codex_root = self._root(codex_root, default_codex, "sessions")
        self.claude_root = self._root(claude_root, default_claude, "projects")
        self.bridge_root = Path(bridge_root) if bridge_root is not None else None
        self.errors: list[str] = []
        self._records: dict[Path, _Record] = {}
        self._completions: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _root(explicit: Path | None, default: Path, child: str) -> Path:
        if explicit is None:
            return default / child
        root = Path(explicit).expanduser()
        return root / child if (root / child).is_dir() else root

    def poll(self) -> list[Session]:
        self.errors = []
        candidates: list[tuple[float, Path, str]] = []
        for provider, root in (("codex", self.codex_root), ("claude", self.claude_root)):
            try:
                for path in root.rglob("*.jsonl"):
                    if "subagents" in path.parts or "subagent" in path.parts:
                        continue
                    try:
                        candidates.append((path.stat().st_mtime, path, provider))
                    except OSError:
                        continue
            except OSError:
                self.errors.append(f"{provider}: 无法读取本地会话目录")
        candidates.sort(key=lambda item: item[0], reverse=True)
        paths = {path for _, path, _ in candidates}
        self._records = {path: record for path, record in self._records.items() if path in paths}
        snapshots: dict[tuple[str, str], Session] = {}
        now = time.time()
        visited: set[Path] = set()
        for modified, path, provider in candidates:
            visited.add(path)
            record = self._records.setdefault(path, _Record(path, provider))
            try:
                self._read(record, modified)
            except (OSError, ValueError):
                self.errors.append(f"{provider}: 一个会话文件暂时无法读取")
                continue
            if record.malformed:
                self.errors.append(f"{provider}: 已跳过 {record.malformed} 条损坏或过大的日志记录")
            if record.excluded or record.session is None:
                continue
            session = replace(record.session, quota=[replace(q) for q in record.session.quota])
            if not record.eof:
                session.usage_partial = True
            if session.state == "working" and now - session.updated_at > _STALE_SECONDS:
                session.state = "unknown"
                session.detail = "任务日志已超过 5 分钟未更新；无法确认 agent 是否仍在工作"
            key = (session.provider, session.id)
            previous = snapshots.get(key)
            if previous is None or previous.updated_at < session.updated_at:
                snapshots[key] = session
            if len(snapshots) >= _MAX_SESSIONS:
                break
        self._records = {path: record for path, record in self._records.items() if path in visited}
        sessions = list(snapshots.values())
        if self.bridge_root is not None:
            from .bridge import apply_bridge
            sessions = apply_bridge(sessions, self.bridge_root)
        for session in sessions:
            self._reconcile_completion(session)
        return sorted(sessions, key=lambda session: session.updated_at, reverse=True)[:_MAX_SESSIONS]

    def _reconcile_completion(self, session: Session) -> None:
        """Associate log/hook confirmations using turn evidence, never a cooldown.

        A transcript may be flushed after Stop. A new prompt that started after
        the prior completion (or a new identity from an already seen source)
        is a new turn even when both turns finish inside one polling interval.
        """
        if session.provider != "claude" or not session.completion_id:
            return
        key = (session.provider, session.id)
        candidate = session.completion_id
        previous = self._completions.get(key)
        source = candidate.split(":", 1)[0]
        if previous is not None:
            aliases = previous["aliases"]
            same = candidate in aliases
            same_source = any(alias.split(":", 1)[0] == source for alias in aliases)
            newer_turn = session.completion_turn_started_at > previous["completed_at"]
            newer_identity = (same_source and not same and session.completed_at >= previous["completed_at"])
            if same or not (newer_turn or newer_identity):
                aliases.add(candidate)
                session.completion_id = previous["id"]
                session.completed_at = previous["completed_at"]
                session.completion_turn_started_at = previous["started_at"]
                return
        self._completions[key] = {"id": candidate, "completed_at": session.completed_at,
                                  "started_at": session.completion_turn_started_at, "aliases": {candidate}}

    def _read(self, record: _Record, modified: float) -> None:
        with record.path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            reset = record.identity is not None and record.identity != identity
            reset = reset or stat.st_size < record.offset
            if record.checkpoint and not reset:
                stream.seek(record.offset - len(record.checkpoint))
                reset = stream.read(len(record.checkpoint)) != record.checkpoint
            if reset:
                record.reset()
            record.identity = identity
            if record.excluded:
                record.eof = True
                return
            stream.seek(record.offset)
            budget = _FILE_READ_BUDGET
            while budget > 0:
                chunk = stream.read(min(_READ_CHUNK, budget))
                if not chunk:
                    break
                record.offset += len(chunk)
                budget -= len(chunk)
                record.checkpoint = (record.checkpoint + chunk)[-128:]
                self._consume(record, chunk, modified)
                if record.excluded:
                    break
            record.eof = record.offset >= stat.st_size

    def _consume(self, record: _Record, chunk: bytes, modified: float) -> None:
        lines = (record.pending + chunk).split(b"\n")
        record.pending = lines.pop()
        for line in lines:
            if record.overlong:
                record.overlong = False
                continue
            if len(line) > _MAX_LINE:
                record.malformed += 1
                continue
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                record.malformed += 1
                continue
            if not isinstance(item, dict):
                record.malformed += 1
                continue
            record.line_count += 1
            if record.provider == "codex":
                self._codex(record, item, modified)
            else:
                self._claude(record, item, modified)
        if len(record.pending) > _MAX_LINE:
            record.pending = b""
            record.overlong = True
            record.malformed += 1

    @staticmethod
    def _state(session: Session, state: str, stamp: float, detail: str = "") -> None:
        if stamp >= session.updated_at:
            session.state = state
            session.updated_at = stamp
            session.detail = detail

    @staticmethod
    def _start_turn(session: Session, identity: str, stamp: float) -> None:
        if stamp >= session.turn_started_at:
            session.turn_id, session.turn_started_at = identity, stamp

    @staticmethod
    def _complete_turn(session: Session, identity: str, stamp: float) -> None:
        identity = session.turn_id or identity
        if identity != session.completion_id and stamp >= session.completed_at:
            session.completion_id = identity
            session.completed_at = stamp
            session.completion_turn_started_at = session.turn_started_at

    def _codex(self, record: _Record, item: dict, modified: float) -> None:
        kind = item.get("type")
        payload = _mapping(item.get("payload"))
        stamp = _timestamp(item.get("timestamp"), modified)
        if kind == "session_meta":
            # Forked logs can contain their parent's metadata after their own.
            if record.meta_seen:
                return
            record.meta_seen = True
            source = payload.get("source")
            if isinstance(source, dict) or source == "subagent":
                record.excluded = True
                return
            cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
            identity = str(payload.get("id") or record.path.stem)
            record.session = Session(
                id=identity, provider="codex", source=_source(source, payload.get("originator")),
                name=_project_name(cwd, "Codex"), cwd=cwd, state="idle", updated_at=stamp,
            )
            return
        session = record.session
        if session is None or record.excluded:
            return
        if kind == "response_item":
            self._codex_response(record, payload, stamp)
            return
        if kind != "event_msg":
            return
        event = payload.get("type")
        states = {
            "task_started": "working", "task_complete": "completed",
            "turn_aborted": "interrupted", "task_failed": "failed",
            "approval_requested": "asking", "request_user_input": "asking",
            "user_message": "working",
        }
        if isinstance(event, str) and event in states:
            turn = payload.get("turn_id")
            identity = "codex-turn:" + (turn if isinstance(turn, str) else f"{stamp:.6f}:{record.line_count}")
            if event == "task_started":
                self._start_turn(session, identity, stamp)
            elif event == "task_complete":
                self._complete_turn(session, identity, _timestamp(item.get("timestamp"), 0))
            if stamp >= session.updated_at:
                if states[event] == "asking":
                    call = payload.get("call_id")
                    if isinstance(call, str) and call:
                        record.asking_calls.add(call)
                else:
                    record.asking_calls.clear()
            detail = "需要授权、选择或用户输入" if states[event] == "asking" else ""
            self._state(session, states[event], stamp, detail)
        elif event == "token_count":
            info = _mapping(payload.get("info"))
            usage = _mapping(info.get("total_token_usage"))
            if usage:
                session.input_tokens = _count(usage.get("input_tokens"))
                session.output_tokens = _count(usage.get("output_tokens"))
                session.cached_tokens = _count(usage.get("cached_input_tokens"))
                session.total_tokens = _count(usage.get("total_tokens")) or (
                    session.input_tokens + session.output_tokens
                )
            context_size = _number(info.get("model_context_window"))
            last = _mapping(info.get("last_token_usage"))
            if context_size > 0 and "total_tokens" in last:
                session.context_percent = min(100.0, max(0.0, _number(last["total_tokens"]) / context_size * 100))
                session.context_observed_at = stamp
            limits = _mapping(payload.get("rate_limits"))
            if limits:
                quota = []
                for key in ("primary", "secondary"):
                    window = _mapping(limits.get(key))
                    if "used_percent" not in window:
                        continue
                    percent = _percentage(window["used_percent"])
                    if percent is None:
                        continue
                    minutes = _count(window.get("window_minutes"))
                    label = (f"{minutes // 1440} 天" if minutes and minutes % 1440 == 0 else
                             f"{minutes // 60} 小时" if minutes and minutes % 60 == 0 else
                             f"{minutes} 分钟" if minutes else ("主要限额" if key == "primary" else "次要限额"))
                    reset = window.get("resets_at")
                    quota.append(QuotaWindow(
                        label, percent,
                        _timestamp(reset, 0) or None, stamp,
                    ))
                if quota:
                    session.quota = quota
            # Usage events also prove the current turn was recently active.
            if session.state == "working":
                session.updated_at = max(session.updated_at, stamp)

    def _codex_response(self, record: _Record, payload: dict, stamp: float) -> None:
        """Identify explicit blocking question tools, never question-like prose.

        Only the matching tool output resolves a tracked question; a parallel
        unrelated tool result must not hide a question still awaiting input.
        """
        session = record.session
        if session is None or stamp < session.updated_at:
            return
        kind = payload.get("type")
        call = payload.get("call_id")
        call = call if isinstance(call, str) and call else ""
        if kind in ("function_call", "custom_tool_call"):
            if payload.get("status") not in (None, "completed"):
                return
            if payload.get("name") in ("request_user_input", "functions.request_user_input"):
                if call:
                    record.asking_calls.add(call)
                    self._state(session, "asking", stamp, "需要用户选择或输入")
                return
        elif kind in ("function_call_output", "custom_tool_call_output"):
            if call in record.asking_calls:
                record.asking_calls.remove(call)
                if not record.asking_calls:
                    self._state(session, "working", stamp, "用户输入请求已返回，继续工作")
                return
        elif kind == "message" and payload.get("role") == "user":
            record.asking_calls.clear()
            if session.state in ("asking", "waiting"):
                self._state(session, "working", stamp)
            return
        elif kind not in ("message", "agent_message", "reasoning"):
            return
        if not record.asking_calls and session.state in ("asking", "waiting", "working"):
            self._state(session, "working", stamp)

    def _claude(self, record: _Record, item: dict, modified: float) -> None:
        if item.get("isSidechain") is True:
            return
        kind = item.get("type")
        if not isinstance(kind, str) or kind not in {"user", "assistant", "system"}:
            return
        stamp = _timestamp(item.get("timestamp"), modified)
        if record.session is None:
            cwd = item.get("cwd") if isinstance(item.get("cwd"), str) else ""
            record.session = Session(
                id=str(item.get("sessionId") or record.path.stem), provider="claude",
                source=_source(item.get("entrypoint"), item.get("source")),
                name=_project_name(cwd, "Claude Code"), cwd=cwd,
                state="idle", updated_at=stamp,
            )
        session = record.session
        if isinstance(item.get("cwd"), str) and item["cwd"]:
            session.cwd = item["cwd"]
            session.name = _project_name(session.cwd, "Claude Code")
        source = _source(item.get("entrypoint"), item.get("source"))
        if source != "Unknown":
            session.source = source
        message = _mapping(item.get("message"))
        if item.get("isApiErrorMessage") is True or (kind == "system" and item.get("subtype") == "api_error"):
            self._state(session, "failed", stamp, "本地会话记录了 API 错误")
            return
        if kind == "user":
            # Match only the CLI's exact interruption sentinel; never expose text.
            content = message.get("content")
            interrupted = isinstance(content, str) and content in {
                "[Request interrupted by user]", "[Request interrupted by user for tool use]",
            }
            if isinstance(content, list):
                interrupted = any(
                    isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) and part.get("text") in {
                        "[Request interrupted by user]", "[Request interrupted by user for tool use]",
                    } for part in content if isinstance(part, dict)
                )
            self._state(session, "interrupted" if interrupted else "working", stamp)
            tool_result = isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "tool_result" for part in content)
            if not interrupted and not tool_result and not item.get("isMeta") and not item.get("isCompactSummary"):
                prompt_id = item.get("promptId") or item.get("prompt_id")
                identity = ("prompt:" + prompt_id if isinstance(prompt_id, str) else
                            "claude-log:" + str(item.get("uuid") or f"{stamp:.6f}:{record.line_count}"))
                self._start_turn(session, identity, stamp)
        elif kind == "assistant":
            usage = _mapping(message.get("usage"))
            identity = message.get("id") or item.get("uuid")
            if usage:
                # Anthropic reports cache creation/read separately from input.
                cached = _count(usage.get("cache_read_input_tokens"))
                tokens = (
                    _count(usage.get("input_tokens")) + cached + _count(usage.get("cache_creation_input_tokens")),
                    _count(usage.get("output_tokens")), cached,
                )
                if not message.get("id"):
                    session.usage_partial = True
                if identity is None:
                    identity = f"anonymous:{record.offset}:{len(record.messages)}"
                    session.usage_partial = True
                previous = record.messages.get(str(identity), (0, 0, 0))
                # Streaming snapshots may omit a previously reported counter.
                tokens = tuple(max(old, new) for old, new in zip(previous, tokens))
                record.messages[str(identity)] = tokens
                session.input_tokens += tokens[0] - previous[0]
                session.output_tokens += tokens[1] - previous[1]
                session.cached_tokens += tokens[2] - previous[2]
                session.total_tokens = session.input_tokens + session.output_tokens
            stop = message.get("stop_reason")
            state = "completed" if isinstance(stop, str) and stop in {"end_turn", "stop_sequence"} else "working"
            detail = ""
            if stop == "max_tokens":
                state, detail = "interrupted", "输出达到 token 上限，本轮响应被截断"
            if state == "completed":
                self._complete_turn(session, "claude-log:" + str(identity or f"{stamp:.6f}:{record.line_count}"),
                                    _timestamp(item.get("timestamp"), 0))
            self._state(session, state, stamp, detail)

