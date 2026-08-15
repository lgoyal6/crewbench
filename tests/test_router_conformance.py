"""Conformance: our port must behave like groundcrew's router.

Every case below is transcribed from groundcrew's own test files at 32346b9:
  src/commands/eligibility.test.ts   (describe pickBestAgent / classifyUsageExhaustion)
  src/commands/dispatcher.test.ts    (describe "weekly paced budget")

Run: python -m pytest tests/ -q     (or: python tests/test_router_conformance.py)
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crewbench.router import (  # noqa: E402
    Usage,
    classify_usage_exhaustion,
    exhausted_usage,
    pick_best_agent,
    weekly_paced_budget_percentage,
)

DEFS = ["claude", "codex"]
DEFAULT = "claude"
MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY


def day_end(n: int) -> int:
    """dispatcher.test.ts: dayEnd(n) = MINUTES_PER_WEEK - n * MINUTES_PER_DAY."""
    return MINUTES_PER_WEEK - n * MINUTES_PER_DAY


# --- pickBestAgent (eligibility.test.ts) ------------------------------------


def test_returns_none_when_every_agent_is_exhausted():
    assert pick_best_agent(DEFS, DEFAULT, {}, {"claude", "codex"}) is None


def test_falls_back_to_default_when_no_usage_data():
    assert pick_best_agent(DEFS, DEFAULT, {}, set()) == "claude"


def test_breaks_ties_in_favor_of_default():
    usage = {
        "claude": Usage(session=0.5, session_end_duration=30),
        "codex": Usage(session=0.5, session_end_duration=30),
    }
    assert pick_best_agent(DEFS, DEFAULT, usage, set()) == "claude"
    # and the tiebreak follows agents.default, not the dict order
    assert pick_best_agent(DEFS, "codex", usage, set()) == "codex"


def test_picks_lowest_session_score():
    usage = {
        "claude": Usage(session=0.7, session_end_duration=30),
        "codex": Usage(session=0.3, session_end_duration=30),
    }
    assert pick_best_agent(DEFS, DEFAULT, usage, set()) == "codex"


# --- classifyUsageExhaustion, session gate (eligibility.test.ts) ------------


def test_reports_session_exhaustion():
    out = classify_usage_exhaustion({"claude": Usage(session=0.95, session_end_duration=30)})
    assert len(out) == 1
    e = out[0]
    assert (e.kind, e.agent, e.used_percentage, e.limit_percentage, e.reset_minutes) == (
        "session",
        "claude",
        95.0,
        85.0,
        30,
    )


def test_exhausted_usage_sentinel_gates_even_at_limit_100():
    # usage.ts: EXHAUSTED_USAGE pins both windows to Infinity so the strict `>`
    # fires at every legal threshold, including sessionLimitPercentage: 100.
    out = classify_usage_exhaustion({"claude": exhausted_usage()}, session_limit_percentage=100.0)
    assert [e.kind for e in out] == ["session"]
    assert math.isinf(out[0].used_percentage)


def test_unavailable_short_circuits_and_does_not_double_gate():
    # dispatcher.test.ts: "does not double-gate when weekly is Infinity"
    snapshot = exhausted_usage("codexbar returned no usage for provider=claude, source=oauth")
    out = classify_usage_exhaustion({"claude": snapshot})
    assert [e.kind for e in out] == ["unavailable"]
    assert out[0].reason.startswith("codexbar returned no usage")


# --- weekly paced budget (dispatcher.test.ts) ------------------------------


def test_does_not_gate_below_the_current_day_budget():
    # End of day 3 -> 3/7 = 42.86% allowed. Used 30%.
    out = classify_usage_exhaustion(
        {"claude": Usage(session=0.1, session_end_duration=30, weekly=0.3, week_end_duration=day_end(3))}
    )
    assert out == []


def test_allows_first_day_budget_immediately_after_rollover():
    # 19 minutes after rollover is still day 1, so 1/7 = 14.29% is allowed.
    out = classify_usage_exhaustion(
        {
            "codex": Usage(
                session=0.1,
                session_end_duration=30,
                weekly=0.01,
                week_end_duration=MINUTES_PER_WEEK - 19,
            )
        }
    )
    assert out == []


def test_gates_when_weekly_exceeds_the_current_day_budget():
    # End of day 1 -> 1/7 = 14.29% allowed. Used 20%.
    out = classify_usage_exhaustion(
        {"claude": Usage(session=0.1, session_end_duration=30, weekly=0.2, week_end_duration=day_end(1))}
    )
    assert [e.kind for e in out] == ["weekly"]
    e = out[0]
    # groundcrew logs: "claude weekly at 20.0% (> 14.3% paced budget)"
    assert f"{e.used_percentage:.1f}" == "20.0"
    assert f"{e.allowed_percentage:.1f}" == "14.3"
    assert e.reset_minutes == day_end(1)


def test_equality_does_not_gate():
    # Contract is strict `>`. Mid-week (3.5 days in) is day 4's bucket, used 4/7.
    out = classify_usage_exhaustion(
        {
            "claude": Usage(
                session=0.1,
                session_end_duration=30,
                weekly=4 / 7,
                week_end_duration=MINUTES_PER_WEEK / 2,
            )
        }
    )
    assert out == []


def test_permits_catch_up_usage_when_behind_pace():
    # 25% used at end of day 2 -> allowed 28.57%.
    out = classify_usage_exhaustion(
        {"claude": Usage(session=0.1, session_end_duration=30, weekly=0.25, week_end_duration=day_end(2))}
    )
    assert out == []


def test_null_weekly_is_ignored():
    out = classify_usage_exhaustion(
        {"claude": Usage(session=0.1, session_end_duration=30, weekly=None, week_end_duration=None)}
    )
    assert out == []


def test_null_week_end_duration_is_ignored():
    # Without weekEndDuration the gate stays open even at 99%.
    out = classify_usage_exhaustion(
        {"claude": Usage(session=0.1, session_end_duration=30, weekly=0.99, week_end_duration=None)}
    )
    assert out == []


def test_clamps_out_of_range_week_end_duration_to_the_first_day_bucket():
    out = classify_usage_exhaustion(
        {
            "claude": Usage(
                session=0.1,
                session_end_duration=30,
                weekly=0.2,
                week_end_duration=MINUTES_PER_WEEK + 5000,
            )
        }
    )
    assert [e.kind for e in out] == ["weekly"]
    assert f"{out[0].allowed_percentage:.1f}" == "14.3"


def test_paced_budget_curve():
    assert math.isclose(weekly_paced_budget_percentage(day_end(0)), 100 / 7)
    assert math.isclose(weekly_paced_budget_percentage(day_end(7)), 100.0)
    # Negative week_end_duration (clock skew) clamps to a full week elapsed.
    assert math.isclose(weekly_paced_budget_percentage(-5000), 100.0)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
