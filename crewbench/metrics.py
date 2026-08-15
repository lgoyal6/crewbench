"""Aggregation: p50/p99 wall clock, tokens and dollars per task, success rate, PR-rate."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .runners import Observation

MILLION = 1_000_000


@dataclass(frozen=True)
class Prices:
    """Dollars per million tokens. Placeholders until you set your own.

    The three-way split matters and is the whole reason this is not one number:
    cached input is an order of magnitude cheaper than fresh input, and an agent
    session that re-reads a large repo prefix is almost entirely cached input.
    Billing every input token at the cache-miss rate overstates cost several-fold.
    """

    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    source: str

    @staticmethod
    def load(path: Path) -> "Prices":
        data = json.loads(path.read_text())
        return Prices(
            input_per_million=data["input_per_million"],
            cached_input_per_million=data["cached_input_per_million"],
            output_per_million=data["output_per_million"],
            source=data.get("source", "unlabelled"),
        )

    def cost(self, obs: Observation) -> float:
        return (
            obs.input_tokens * self.input_per_million
            + obs.cached_input_tokens * self.cached_input_per_million
            + obs.output_tokens * self.output_per_million
        ) / MILLION


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile. No numpy, no interpolation ambiguity."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    import math

    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass
class Summary:
    label: str
    n: int
    success_rate: float
    pr_rate: float
    p50_wall_seconds: float
    p99_wall_seconds: float
    mean_tokens_per_task: float
    mean_cost_per_task: float
    total_cost: float
    cost_per_success: float
    cost_per_pr: float
    blended_dollars_per_million: float


def summarize(label: str, observations: list[Observation], prices: Prices) -> Summary:
    n = len(observations)
    if n == 0:
        return Summary(label, 0, *[float("nan")] * 9)
    successes = sum(1 for o in observations if o.success)
    prs = sum(1 for o in observations if o.pr_opened)
    costs = [prices.cost(o) for o in observations]
    total_cost = sum(costs)
    total_tokens = sum(o.total_tokens for o in observations)
    walls = [o.wall_clock_seconds for o in observations]
    return Summary(
        label=label,
        n=n,
        success_rate=successes / n,
        pr_rate=prs / n,
        p50_wall_seconds=percentile(walls, 50),
        p99_wall_seconds=percentile(walls, 99),
        mean_tokens_per_task=total_tokens / n,
        mean_cost_per_task=total_cost / n,
        total_cost=total_cost,
        cost_per_success=total_cost / successes if successes else float("nan"),
        cost_per_pr=total_cost / prs if prs else float("nan"),
        blended_dollars_per_million=total_cost / (total_tokens / MILLION) if total_tokens else 0.0,
    )
