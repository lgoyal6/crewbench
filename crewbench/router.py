"""Faithful Python port of groundcrew's agent-any routing, plus alternative policies.

Ported from ClipboardHealth/groundcrew @ 32346b9 (2026-08-13):
  - src/commands/eligibility.ts :: pickBestAgent
  - src/commands/eligibility.ts :: classifyUsageExhaustion
  - src/commands/eligibility.ts :: weeklyPacedBudgetPercentage

The port exists so the benchmark's baseline is *their* router rather than a paraphrase.
tests/test_router_conformance.py replays the cases from groundcrew's own
eligibility.test.ts and dispatcher.test.ts against it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY
DAYS_PER_WEEK = 7
PERCENT = 100.0

# groundcrew's DEFAULT_ORCHESTRATOR.sessionLimitPercentage (src/lib/config.ts).
DEFAULT_SESSION_LIMIT_PERCENTAGE = 85.0


@dataclass
class Usage:
    """Port of NormalizedUsage (src/lib/usage.ts). Fractions, not percentages."""

    session: float | None = None
    session_end_duration: float | None = None
    weekly: float | None = None
    week_end_duration: float | None = None
    unavailable_reason: str | None = None


# Port of EXHAUSTED_USAGE: both windows pinned to +inf so the strict `>` check
# fires even at sessionLimitPercentage == 100.
def exhausted_usage(reason: str | None = None) -> Usage:
    return Usage(
        session=math.inf,
        session_end_duration=None,
        weekly=math.inf,
        week_end_duration=None,
        unavailable_reason=reason,
    )


def weekly_paced_budget_percentage(week_end_duration: float) -> float:
    """Port of weeklyPacedBudgetPercentage. Day 1's budget opens at rollover."""
    elapsed_minutes = min(MINUTES_PER_WEEK, max(0.0, MINUTES_PER_WEEK - week_end_duration))
    elapsed_day_count = math.ceil(elapsed_minutes / MINUTES_PER_DAY)
    budget_day_count = min(DAYS_PER_WEEK, max(1, elapsed_day_count))
    return (budget_day_count / DAYS_PER_WEEK) * PERCENT


@dataclass
class Exhaustion:
    kind: str  # "session" | "weekly" | "unavailable"
    agent: str
    used_percentage: float | None = None
    limit_percentage: float | None = None
    allowed_percentage: float | None = None
    reset_minutes: float | None = None
    reason: str | None = None


def classify_usage_exhaustion(
    usage_by_agent: dict[str, Usage],
    session_limit_percentage: float = DEFAULT_SESSION_LIMIT_PERCENTAGE,
) -> list[Exhaustion]:
    """Port of classifyUsageExhaustion. Order matters: unavailable short-circuits."""
    out: list[Exhaustion] = []
    for agent, snapshot in usage_by_agent.items():
        if snapshot.unavailable_reason is not None:
            out.append(Exhaustion(kind="unavailable", agent=agent, reason=snapshot.unavailable_reason))
            continue
        if snapshot.session is not None and snapshot.session * PERCENT > session_limit_percentage:
            out.append(
                Exhaustion(
                    kind="session",
                    agent=agent,
                    used_percentage=snapshot.session * PERCENT,
                    limit_percentage=session_limit_percentage,
                    reset_minutes=snapshot.session_end_duration,
                )
            )
        if (
            snapshot.weekly is not None
            and math.isfinite(snapshot.weekly)
            and snapshot.week_end_duration is not None
        ):
            used = snapshot.weekly * PERCENT
            allowed = weekly_paced_budget_percentage(snapshot.week_end_duration)
            if used > allowed:
                out.append(
                    Exhaustion(
                        kind="weekly",
                        agent=agent,
                        used_percentage=used,
                        allowed_percentage=allowed,
                        reset_minutes=snapshot.week_end_duration,
                    )
                )
    return out


def exhausted_agents(
    usage_by_agent: dict[str, Usage],
    session_limit_percentage: float = DEFAULT_SESSION_LIMIT_PERCENTAGE,
) -> set[str]:
    return {e.agent for e in classify_usage_exhaustion(usage_by_agent, session_limit_percentage)}


def pick_best_agent(
    definitions: list[str],
    default_agent: str,
    usage_by_agent: dict[str, Usage],
    exhausted: set[str],
) -> str | None:
    """Port of pickBestAgent.

    Score is `usage[agent].session` with null/missing treated as 0 (maximum headroom),
    so with no usage data every agent ties at 0 and the default wins the tiebreak.
    """
    candidates = [name for name in definitions if name not in exhausted]
    if not candidates:
        return None
    best_name = candidates[0]
    best_score = _score(usage_by_agent, candidates[0])
    for name in candidates[1:]:
        score = _score(usage_by_agent, name)
        if score < best_score:
            best_name, best_score = name, score
        elif score == best_score and name == default_agent:
            best_name, best_score = name, score
    return best_name


def _score(usage_by_agent: dict[str, Usage], name: str) -> float:
    snapshot = usage_by_agent.get(name)
    if snapshot is None or snapshot.session is None:
        return 0.0
    return snapshot.session


# ---------------------------------------------------------------------------
# Policies under test. Every policy sees the same gates; they differ only in
# which un-gated backend they choose.
# ---------------------------------------------------------------------------


class Policy:
    name = "policy"

    def choose(self, task, definitions, default_agent, usage_by_agent, exhausted) -> str | None:
        raise NotImplementedError


class HeadroomPolicy(Policy):
    """groundcrew's shipped agent-any behaviour, unchanged."""

    name = "headroom"

    def choose(self, task, definitions, default_agent, usage_by_agent, exhausted):
        return pick_best_agent(definitions, default_agent, usage_by_agent, exhausted)


class PinnedPolicy(Policy):
    """Single-backend baseline. Skips the task when its backend is gated."""

    def __init__(self, agent: str):
        self.agent = agent
        self.name = f"{agent}-only"

    def choose(self, task, definitions, default_agent, usage_by_agent, exhausted):
        return None if self.agent in exhausted else self.agent


class MeasuredPolicy(Policy):
    """Routes on measured success-per-dollar per task category.

    `table` maps category -> ordered backend preference, fitted on a held-out
    calibration prefix of the task stream (see bench.py). Falls back to
    groundcrew's headroom pick when every preferred backend is gated, so it can
    never dispatch into a depleted window.
    """

    name = "measured"

    def __init__(self, table: dict[str, list[str]]):
        self.table = table

    def choose(self, task, definitions, default_agent, usage_by_agent, exhausted):
        for agent in self.table.get(task.category, []):
            if agent not in exhausted and agent in definitions:
                return agent
        return pick_best_agent(definitions, default_agent, usage_by_agent, exhausted)
