"""Counterfactual router replay.

Runs one task stream through several routing policies under identical session and
weekly gates, so the cost of the routing decision itself is separable from the cost
of the work.

The gates are groundcrew's, ported in router.py. What is modelled here is the thing
groundcrew reads but does not own: the usage timeline that codexbar reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .router import (
    DEFAULT_SESSION_LIMIT_PERCENTAGE,
    MINUTES_PER_WEEK,
    Usage,
    exhausted_agents,
)
from .runners import Observation
from .tasks import Task


@dataclass(frozen=True)
class PlanLimits:
    """SYNTHETIC PLAN PARAMETERS.

    The 5-hour and weekly window structure is real: Rocky Warren's post states
    "Claude Code Team and Codex Pro both have 5-hour and weekly limits". The
    *capacities* below are invented. They are set to put the default 200-task run
    in a tight regime where both gates fire for a single-backend policy; in a loose
    regime nothing gates and every policy converges, which is a real and reportable
    outcome (`--session-capacity`, `--weekly-capacity`). Put your own plan's numbers
    here before believing any gating result.
    """

    session_window_minutes: int = 300  # 5 hours
    session_capacity_tokens: int = 420_000_000
    weekly_capacity_tokens: int = 3_600_000_000


@dataclass
class ReplayResult:
    policy: str
    observations: list[Observation] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (task_id, reason)
    dispatched_by_backend: dict[str, int] = field(default_factory=dict)


class _UsageClock:
    def __init__(self, backends: list[str], limits: PlanLimits):
        self.limits = limits
        self.session_tokens = {b: 0 for b in backends}
        self.weekly_tokens = {b: 0 for b in backends}
        self.session_window = 0
        self.week_index = 0

    def advance(self, elapsed_minutes: float) -> None:
        window = int(elapsed_minutes // self.limits.session_window_minutes)
        if window != self.session_window:
            self.session_window = window
            for b in self.session_tokens:
                self.session_tokens[b] = 0
        week = int(elapsed_minutes // MINUTES_PER_WEEK)
        if week != self.week_index:
            self.week_index = week
            for b in self.weekly_tokens:
                self.weekly_tokens[b] = 0

    def snapshot(self, elapsed_minutes: float) -> dict[str, Usage]:
        window_start = self.session_window * self.limits.session_window_minutes
        session_end = window_start + self.limits.session_window_minutes - elapsed_minutes
        week_end = MINUTES_PER_WEEK - (elapsed_minutes % MINUTES_PER_WEEK)
        return {
            backend: Usage(
                session=self.session_tokens[backend] / self.limits.session_capacity_tokens,
                session_end_duration=max(0.0, session_end),
                weekly=self.weekly_tokens[backend] / self.limits.weekly_capacity_tokens,
                week_end_duration=week_end,
            )
            for backend in self.session_tokens
        }

    def charge(self, backend: str, tokens: int) -> None:
        self.session_tokens[backend] += tokens
        self.weekly_tokens[backend] += tokens


def replay(
    tasks: list[Task],
    runner,
    policy,
    backends: list[str],
    default_agent: str,
    limits: PlanLimits,
    session_limit_percentage: float = DEFAULT_SESSION_LIMIT_PERCENTAGE,
) -> ReplayResult:
    clock = _UsageClock(backends, limits)
    result = ReplayResult(policy=policy.name)
    result.dispatched_by_backend = {b: 0 for b in backends}

    for task in tasks:
        elapsed_minutes = task.arrival_seconds / 60.0
        clock.advance(elapsed_minutes)
        usage = clock.snapshot(elapsed_minutes)
        exhausted = exhausted_agents(usage, session_limit_percentage)
        chosen = policy.choose(task, backends, default_agent, usage, exhausted)
        if chosen is None:
            reason = "all backends gated" if exhausted else "no candidate"
            result.skipped.append((task.task_id, reason))
            continue
        obs = runner.run(task, chosen)
        result.observations.append(obs)
        result.dispatched_by_backend[chosen] += 1
        # Usage is charged at dispatch rather than at completion. That is
        # conservative (it gates earlier than reality) and it keeps the replay a
        # pure function of the trace.
        clock.charge(chosen, obs.total_tokens)

    return result


def fit_measured_table(
    calibration: list[Observation], prices, backends: list[str]
) -> dict[str, list[str]]:
    """Rank backends per category by measured successes per dollar.

    Fitted only on the calibration split. Calibration is not free: it needs both
    arms run on the same tasks, so it costs len(calibration_tasks) x len(backends)
    real sessions before the policy can be used.
    """
    by_key: dict[tuple[str, str], list[Observation]] = {}
    for obs in calibration:
        by_key.setdefault((obs.category, obs.backend), []).append(obs)

    table: dict[str, list[str]] = {}
    categories = {obs.category for obs in calibration}
    for category in categories:
        scored = []
        for backend in backends:
            group = by_key.get((category, backend), [])
            if not group:
                continue
            cost = sum(prices.cost(o) for o in group)
            successes = sum(1 for o in group if o.success)
            scored.append((successes / cost if cost else 0.0, backend))
        scored.sort(reverse=True)
        table[category] = [backend for _, backend in scored]
    return table
