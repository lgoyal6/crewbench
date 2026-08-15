"""Pluggable runners.

A Runner turns (task, backend) into one Observation. Two implementations ship:

  ReplayRunner  default. Reads a JSONL trace. Offline, deterministic, zero spend.
  CrewRunner    drives the real `crew` binary. Costs money on your own plan.

Swapping is one flag: `--runner crew`. Nothing else in the harness changes.

Every Observation carries `synthetic`. The reporter ORs that flag across the whole
run and stamps SYNTHETIC on every derived number, so a simulated figure cannot be
printed unlabelled even by accident.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Observation:
    task_id: str
    category: str
    backend: str
    wall_clock_seconds: float
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    success: bool
    pr_opened: bool
    synthetic: bool

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cached_input_tokens + self.output_tokens


def write_trace(path: Path, observations: list[Observation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for obs in observations:
            handle.write(json.dumps(asdict(obs), sort_keys=True) + "\n")


class ReplayRunner:
    """Replays a JSONL trace. The default runner."""

    kind = "replay"

    def __init__(self, trace_path: Path):
        self._by_key: dict[tuple[str, str], Observation] = {}
        with trace_path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                obs = Observation(**record)
                self._by_key[(obs.task_id, obs.backend)] = obs
        if not self._by_key:
            raise ValueError(f"empty trace: {trace_path}")
        self.trace_path = trace_path

    def run(self, task, backend: str) -> Observation:
        try:
            return self._by_key[(task.task_id, backend)]
        except KeyError:
            raise KeyError(
                f"trace has no observation for ({task.task_id}, {backend}). "
                "Counterfactual replay needs a full-factorial trace: every task "
                "observed on every backend."
            ) from None

    def observations(self) -> list[Observation]:
        return list(self._by_key.values())


class CrewRunner:
    """Drives the real groundcrew binary. NOT EXERCISED in the shipped results.

    Requires: `crew` on PATH, a configured task source, and an agent CLI that will
    spend real quota. One task, one worktree, one PR.

    What it does per (task, backend):

      1. `crew task create "<title>" --source <source> --agent <backend>`
         (or point `--task-id` at a ticket you already filed)
      2. `crew start <TASK>`
      3. poll `crew status --json --local-only` until the task's `lifecycle` leaves
         "running"/"provisioning" or its `session` stops being "live".
         Wall clock = `updatedAt - startedAt` from that document. groundcrew stores
         instants, never durations, deliberately; the reader subtracts.
      4. `crew status --json` once, and look the task's worktree dir up in
         `payload.pullRequestsByWorktree` (keyed by absolute worktree path) to get
         pr_opened.

    THE ONE GAP: groundcrew records no token or cost field anywhere. RunState has
    agent/state/createdAt/updatedAt; LocalStatusDocument has lifecycle/startedAt;
    neither has tokens. So `token_probe` below is a caller-supplied shell command
    that must print `{"input_tokens":…,"cached_input_tokens":…,"output_tokens":…}`
    for the finished session (e.g. a `codexbar` delta, or the agent CLI's own usage
    output). Without it, cost columns come back zero and the report says so rather
    than guessing.
    """

    kind = "crew"

    def __init__(
        self,
        source: str,
        crew_binary: str = "crew",
        poll_seconds: float = 10.0,
        timeout_seconds: float = 3 * 60 * 60,
        token_probe: str | None = None,
    ):
        if shutil.which(crew_binary) is None:
            raise RuntimeError(
                f"{crew_binary!r} not on PATH. Install with "
                "`npm install -g @clipboard-health/groundcrew@latest`, then `crew doctor`."
            )
        self.source = source
        self.crew = crew_binary
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.token_probe = token_probe

    def _json(self, *args: str) -> dict:
        out = subprocess.run(
            [self.crew, *args], capture_output=True, text=True, check=True
        ).stdout
        # `crew status --json` prints the local document first, then the remote one.
        decoder, index, documents = json.JSONDecoder(), 0, []
        while index < len(out):
            while index < len(out) and out[index].isspace():
                index += 1
            if index >= len(out):
                break
            value, index = decoder.raw_decode(out, index)
            documents.append(value)
        return {"local": documents[0], "remote": documents[1] if len(documents) > 1 else None}

    def run(self, task, backend: str) -> Observation:
        subprocess.run([self.crew, "start", task.task_id], check=True)
        deadline = time.monotonic() + self.timeout_seconds
        entry: dict = {}
        while time.monotonic() < deadline:
            local = self._json("status", "--json", "--local-only")["local"]
            entry = next(
                (t for t in local["tasks"] if t["task"] == task.task_id.lower()), {}
            )
            if entry and (
                entry.get("lifecycle") not in ("running", "provisioning", "resumed")
                or entry.get("session") in ("exited", "not-live")
            ):
                break
            time.sleep(self.poll_seconds)
        else:
            raise TimeoutError(f"{task.task_id} did not finish within {self.timeout_seconds}s")

        wall = _elapsed_seconds(entry.get("startedAt"), entry.get("updatedAt"))
        remote = self._json("status", "--json")["remote"] or {}
        prs_by_worktree = remote.get("payload", {}).get("pullRequestsByWorktree", {})
        dirs = [w["dir"] for w in entry.get("worktrees", [])]
        pr_opened = any(prs_by_worktree.get(d) for d in dirs)
        tokens = self._probe_tokens()

        return Observation(
            task_id=task.task_id,
            category=task.category,
            backend=backend,
            wall_clock_seconds=wall,
            input_tokens=tokens.get("input_tokens", 0),
            cached_input_tokens=tokens.get("cached_input_tokens", 0),
            output_tokens=tokens.get("output_tokens", 0),
            success=entry.get("lifecycle") not in ("failed-to-launch", "interrupted"),
            pr_opened=pr_opened,
            synthetic=False,
        )

    def _probe_tokens(self) -> dict:
        if self.token_probe is None:
            return {}
        out = subprocess.run(
            self.token_probe, shell=True, capture_output=True, text=True, check=True
        ).stdout
        return json.loads(out)


def _elapsed_seconds(started_at: str | None, updated_at: str | None) -> float:
    from datetime import datetime

    if not started_at or not updated_at:
        return 0.0
    fmt = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return max(0.0, (fmt(updated_at) - fmt(started_at)).total_seconds())
