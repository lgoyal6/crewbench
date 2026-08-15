"""The task suite.

Categories are the four kinds of ticket Rocky Warren names as working well in
"Tickets to Pull Requests While You Sleep" (clipboardworks.com, 2026-05-27):
flaky test fixes, Dependabot/Renovate update failures, stale feature flag cleanup,
and long-tail migrations.

A Task here is a routing unit, not a prompt. crewbench measures what a backend
does with a category of work; the prompt lives in the ticket, exactly as groundcrew
treats it ("the task description is the prompt").
"""

from __future__ import annotations

import random
from dataclasses import dataclass

CATEGORIES = ("flaky_test", "dependency_update", "flag_cleanup", "migration")

# Mix of an overnight backlog. Weights are a modelling choice, documented here so
# they are auditable; change them to match your own board.
CATEGORY_WEIGHTS = {
    "flaky_test": 0.35,
    "dependency_update": 0.25,
    "flag_cleanup": 0.15,
    "migration": 0.25,
}


@dataclass(frozen=True)
class Task:
    task_id: str
    category: str
    # Arrival offset in seconds from the start of the batch. groundcrew polls its
    # source every pollIntervalMilliseconds (default 120_000), so tasks enter the
    # router one tick at a time rather than all at once.
    arrival_seconds: int


def build_suite(count: int, seed: int, poll_interval_seconds: int = 120) -> list[Task]:
    rng = random.Random(seed)
    names = list(CATEGORY_WEIGHTS)
    weights = [CATEGORY_WEIGHTS[n] for n in names]
    tasks: list[Task] = []
    for i in range(count):
        category = rng.choices(names, weights=weights, k=1)[0]
        tasks.append(
            Task(
                task_id=f"{category}-{i:03d}",
                category=category,
                arrival_seconds=i * poll_interval_seconds,
            )
        )
    return tasks
