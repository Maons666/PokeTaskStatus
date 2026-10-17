"""Small, UI-independent models for local agent observations."""

from __future__ import annotations

from dataclasses import dataclass, field
import time


STATES = frozenset({
    "idle", "working", "completed", "waiting", "asking", "failed", "interrupted",
    "disconnected", "unknown",
})


@dataclass
class QuotaWindow:
    label: str
    used_percent: float
    resets_at: float | None = None
    observed_at: float = field(default_factory=time.time)


@dataclass
class Session:
    id: str
    provider: str
    source: str
    name: str
    cwd: str = ""
    state: str = "unknown"
    updated_at: float = field(default_factory=time.time)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    context_percent: float | None = None
    quota: list[QuotaWindow] = field(default_factory=list)
    detail: str = ""
    usage_partial: bool = False
    context_observed_at: float = 0
    turn_id: str = ""
    turn_started_at: float = 0
    completion_id: str = ""
    completed_at: float = 0
    completion_turn_started_at: float = 0

