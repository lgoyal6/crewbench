"""Generates the SYNTHETIC full-factorial trace shipped as the default input.

Read this file before you read any number crewbench prints. Everything the default
run reports is a consequence of the priors below, and the priors are guesses.

Where the priors come from
--------------------------
There is exactly one public statement about relative backend skill in the whole
groundcrew corpus, from Rocky Warren's post (clipboardworks.com, 2026-05-27):

    "I've found Codex is the better debugger and reviewer, and Claude Code is the
     better designer."

That sentence is qualitative and names no task categories. Mapping it onto the four
categories below is MY choice, not theirs: debugging-shaped work (flaky tests,
dependency-update failures) leans codex; design-shaped work (long-tail migrations,
stale flag cleanup) leans claude. The deltas are small and made up.

CONSEQUENCE, stated plainly: the per-backend ranking in the default output is a
property of these priors and is not evidence about Claude Code or Codex. Replace the
trace with real observations (`--runner crew`) and the ranking may invert. What the
default run demonstrates is the measurement plumbing and the size of the routing
question, not the answer.

Scale calibration
-----------------
The only real anchors available are two aggregates from the same post: "50+ PRs a
day" and "9.5B tokens a month, over $7,000 at retail API rates". Those imply roughly
6.3M tokens per PR and a blended ~$0.74 per million tokens.

TOKEN_SCALE below is fitted so the trace's tokens-per-PR lands on the 6.3M anchor.
That is an INPUT, not a result: the harness is being told what magnitude to produce,
so "the trace matches the anchor" proves nothing. The blended $/Mtok figure is not
fitted, so the report prints it against the anchor as a free consistency check.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .runners import Observation
from .tasks import Task


@dataclass(frozen=True)
class Prior:
    p_success: float
    # PR-rate is conditional on success: some categories legitimately finish by
    # filing a follow-up ticket instead of a PR. Rocky's flaky-test flow does
    # exactly that at step 3 (session marks the ticket Done, files a fix plan).
    p_pr_given_success: float
    wall_median_seconds: float
    wall_sigma: float
    cached_median: float
    input_median: float
    output_median: float
    token_sigma: float


# --- SYNTHETIC PRIORS. Every number below is invented. ----------------------
PRIORS: dict[tuple[str, str], Prior] = {
    ("flaky_test", "codex"): Prior(0.82, 0.55, 620, 0.55, 5_800_000, 400_000, 95_000, 0.45),
    ("flaky_test", "claude"): Prior(0.71, 0.58, 540, 0.60, 5_200_000, 370_000, 88_000, 0.45),
    ("dependency_update", "codex"): Prior(0.86, 0.80, 430, 0.50, 3_900_000, 280_000, 60_000, 0.40),
    ("dependency_update", "claude"): Prior(0.79, 0.82, 400, 0.52, 3_700_000, 265_000, 62_000, 0.40),
    ("flag_cleanup", "codex"): Prior(0.80, 0.93, 510, 0.48, 4_400_000, 300_000, 70_000, 0.40),
    ("flag_cleanup", "claude"): Prior(0.88, 0.94, 470, 0.45, 4_100_000, 290_000, 72_000, 0.40),
    ("migration", "codex"): Prior(0.68, 0.90, 1_150, 0.65, 8_600_000, 620_000, 150_000, 0.50),
    ("migration", "claude"): Prior(0.77, 0.91, 1_020, 0.62, 8_100_000, 590_000, 158_000, 0.50),
}
# ---------------------------------------------------------------------------

BACKENDS = ("claude", "codex")

# Fitted so tokens-per-PR matches the 6.3M anchor. See the module docstring: this
# is a calibration input, not a validated result.
TOKEN_SCALE = 0.604


def generate(tasks: list[Task], seed: int) -> list[Observation]:
    """One observation per (task, backend) — full factorial, so the router replay
    can price the road not taken exactly instead of extrapolating."""
    rng = random.Random(seed)
    out: list[Observation] = []
    for task in tasks:
        for backend in BACKENDS:
            prior = PRIORS[(task.category, backend)]
            success = rng.random() < prior.p_success
            pr = success and rng.random() < prior.p_pr_given_success
            wall = rng.lognormvariate(_mu(prior.wall_median_seconds), prior.wall_sigma)
            # A failed run still burns wall clock and tokens; it just burns less of
            # them, because it gives up or the harness kills it.
            scale = 1.0 if success else rng.uniform(0.45, 0.95)
            out.append(
                Observation(
                    task_id=task.task_id,
                    category=task.category,
                    backend=backend,
                    wall_clock_seconds=round(wall * scale, 1),
                    cached_input_tokens=_tokens(rng, prior.cached_median, prior.token_sigma, scale),
                    input_tokens=_tokens(rng, prior.input_median, prior.token_sigma, scale),
                    output_tokens=_tokens(rng, prior.output_median, prior.token_sigma, scale),
                    success=success,
                    pr_opened=pr,
                    synthetic=True,
                )
            )
    return out


def _mu(median: float) -> float:
    import math

    return math.log(median)


def _tokens(rng: random.Random, median: float, sigma: float, scale: float) -> int:
    return int(rng.lognormvariate(_mu(median), sigma) * scale * TOKEN_SCALE)
