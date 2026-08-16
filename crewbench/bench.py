"""crewbench CLI.

  python -m crewbench.bench gen-trace      # regenerate the SYNTHETIC default trace
  python -m crewbench.bench run            # benchmark + counterfactual router replay
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .metrics import Prices, Summary, summarize
from .replay import PlanLimits, fit_measured_table, replay
from .router import HeadroomPolicy, MeasuredPolicy, PinnedPolicy
from .runners import CrewRunner, Observation, ReplayRunner, write_trace
from .simulate import BACKENDS, generate
from .tasks import build_suite

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRACE = ROOT / "traces" / "synthetic-v1.jsonl"
DEFAULT_PRICES = ROOT / "prices.json"

# Anchors from Rocky Warren, "Tickets to Pull Requests While You Sleep",
# clipboardworks.com, 2026-05-27: "50+ PRs a day" and "9.5B tokens a month,
# over $7,000 at retail API rates".
ANCHOR_BLENDED_DOLLARS_PER_MILLION = 7_000 / 9_500  # ≈ 0.737
ANCHOR_TOKENS_PER_PR = 9_500_000_000 / (50 * 30)  # ≈ 6.33M


def _stamp(observations: list[Observation]) -> str:
    return "SYNTHETIC" if any(o.synthetic for o in observations) else "MEASURED"


def _fmt_summary_row(s: Summary, tag: str) -> str:
    return (
        f"| {s.label} | {tag} | {s.n} | {s.success_rate:6.1%} | {s.pr_rate:6.1%} | "
        f"{s.p50_wall_seconds:8.1f} | {s.p99_wall_seconds:9.1f} | "
        f"{s.mean_tokens_per_task / 1e6:7.2f} | ${s.mean_cost_per_task:6.2f} | "
        f"${s.cost_per_success:7.2f} | ${s.cost_per_pr:7.2f} |"
    )


HEADER = (
    "| arm | label | n | success | PR-rate | p50 wall s | p99 wall s | Mtok/task | "
    "$/task | $/success | $/PR |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|"
)


def cmd_gen_trace(args) -> None:
    tasks = build_suite(args.tasks, seed=args.seed)
    observations = generate(tasks, seed=args.seed)
    write_trace(Path(args.out), observations)
    print(f"wrote {len(observations)} SYNTHETIC observations for {len(tasks)} tasks -> {args.out}")


def cmd_run(args) -> None:
    tasks = build_suite(args.tasks, seed=args.seed)
    prices = Prices.load(Path(args.prices))

    if args.runner == "replay":
        runner = ReplayRunner(Path(args.trace))
    elif args.runner == "crew":
        runner = CrewRunner(source=args.source, token_probe=args.token_probe)
    else:
        raise SystemExit(f"unknown runner {args.runner}")

    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)
        print(text)

    # ---- Part 1: the per-backend benchmark ---------------------------------
    per_backend: dict[str, list[Observation]] = {b: [] for b in BACKENDS}
    for task in tasks:
        for backend in BACKENDS:
            per_backend[backend].append(runner.run(task, backend))
    every = [o for group in per_backend.values() for o in group]
    stamp = _stamp(every)

    emit(f"# crewbench: {stamp}")
    emit()
    emit(f"runner={runner.kind}  tasks={len(tasks)}  seed={args.seed}")
    emit(f"prices: {prices.source}")
    emit(
        f"  input ${prices.input_per_million}/Mtok · cached input "
        f"${prices.cached_input_per_million}/Mtok · output ${prices.output_per_million}/Mtok"
    )
    emit()
    emit(f"## 1. Per-backend, full factorial ({stamp})")
    emit()
    emit("Every task run on every backend. This is the table the router needs and")
    emit("groundcrew does not have: it prices the road not taken.")
    emit()
    emit(HEADER)
    summaries = {}
    for backend in BACKENDS:
        s = summarize(backend, per_backend[backend], prices)
        summaries[backend] = s
        emit(_fmt_summary_row(s, stamp))
    emit()

    emit(f"### by task category ({stamp})")
    emit()
    emit(HEADER)
    categories = sorted({t.category for t in tasks})
    for category in categories:
        for backend in BACKENDS:
            group = [o for o in per_backend[backend] if o.category == category]
            emit(_fmt_summary_row(summarize(f"{category}/{backend}", group, prices), stamp))
    emit()

    overall = summarize("all", every, prices)
    emit("### trace scale vs Clipboard's own published aggregates")
    emit()
    emit(
        f"| quantity | this trace ({stamp}) | implied by the blog post (VERIFIED) | fitted? |"
        "\n|---|---|---|---|"
    )
    emit(
        f"| blended $/Mtok | ${overall.blended_dollars_per_million:.3f} | "
        f"${ANCHOR_BLENDED_DOLLARS_PER_MILLION:.3f} | no |"
    )
    prs = sum(1 for o in every if o.pr_opened)
    tokens_per_pr = sum(o.total_tokens for o in every) / prs if prs else float("nan")
    emit(
        f"| tokens per PR | {tokens_per_pr / 1e6:.2f}M | {ANCHOR_TOKENS_PER_PR / 1e6:.2f}M | "
        "yes (TOKEN_SCALE) |"
    )
    emit()
    emit(
        "The token row is fitted, so its agreement proves nothing. The dollar row is "
        "not fitted: it falls out of the token mix and the price file."
    )
    emit()

    # ---- Part 2: counterfactual router replay ------------------------------
    split = max(1, int(len(tasks) * args.calibration_fraction))
    calibration_tasks, evaluation_tasks = tasks[:split], tasks[split:]
    calibration_obs = [
        runner.run(t, b) for t in calibration_tasks for b in BACKENDS
    ]
    table = fit_measured_table(calibration_obs, prices, list(BACKENDS))

    limits = PlanLimits(
        session_capacity_tokens=args.session_capacity,
        weekly_capacity_tokens=args.weekly_capacity,
    )
    policies = [
        HeadroomPolicy(),
        PinnedPolicy("claude"),
        PinnedPolicy("codex"),
        MeasuredPolicy(table),
    ]

    emit(f"## 2. Counterfactual router replay ({stamp})")
    emit()
    emit(
        f"Same {len(tasks)}-task stream, same session gate "
        f"({int(limits.session_window_minutes)}-minute window, "
        "sessionLimitPercentage=85) and same weekly paced budget for every policy."
    )
    emit(
        f"SYNTHETIC plan capacity: {limits.session_capacity_tokens / 1e6:.0f}M tokens per "
        f"session window, {limits.weekly_capacity_tokens / 1e9:.1f}B per week, per backend."
    )
    emit(
        f"`headroom` is a line-by-line port of groundcrew's pickBestAgent. "
        f"`measured` is fitted on the first {split} tasks "
        f"({split * len(BACKENDS)} sessions of calibration spend) and scored on the "
        f"remaining {len(evaluation_tasks)}."
    )
    emit()
    emit("fitted preference table: " + json.dumps(table, sort_keys=True))
    emit()
    emit(
        "| policy | dispatched | skipped (gated) | success | PR-rate | p50 wall s | "
        "p99 wall s | $ total | $/PR | claude/codex split |\n|---|---|---|---|---|---|---|---|---|---|"
    )

    replay_rows = {}
    eval_ids = {t.task_id for t in evaluation_tasks}
    for policy in policies:
        result = replay(tasks, runner, policy, list(BACKENDS), args.default_agent, limits)
        scored = [o for o in result.observations if o.task_id in eval_ids]
        gated = sum(1 for tid, _ in result.skipped if tid in eval_ids)
        s = summarize(policy.name, scored, prices)
        replay_rows[policy.name] = {"summary": asdict(s), "gated": gated}
        split_text = "/".join(str(result.dispatched_by_backend[b]) for b in ("claude", "codex"))
        emit(
            f"| {policy.name} | {s.n} | {gated} | {s.success_rate:.1%} | {s.pr_rate:.1%} | "
            f"{s.p50_wall_seconds:.1f} | {s.p99_wall_seconds:.1f} | ${s.total_cost:.2f} | "
            f"${s.cost_per_pr:.2f} | {split_text} |"
        )
    emit()
    emit(
        "Read the losing axes too: a policy can win on $/PR by skipping hard tasks, "
        "so `dispatched` and `skipped (gated)` belong in the same table as the rates."
    )
    emit()

    # ---- Part 3: how much measurement do you have to buy? ------------------
    full_table = fit_measured_table(every, prices, list(BACKENDS))
    emit(f"## 3. Calibration sample size ({stamp})")
    emit()
    emit(
        "A measured router is only as good as the sample it was fitted on. Each row "
        "fits the preference table on the first N tasks and asks how many of the "
        f"{len(categories)} categories it gets right relative to the full "
        f"{len(tasks)}-task trace."
    )
    emit()
    emit(
        "| calibration tasks | sessions burned (2 arms) | categories ranked correctly |"
        "\n|---|---|---|"
    )
    curve = {}
    for n in (10, 25, 50, 100, len(tasks)):
        if n > len(tasks):
            continue
        sample_ids = {t.task_id for t in tasks[:n]}
        sample = [o for o in every if o.task_id in sample_ids]
        fitted = fit_measured_table(sample, prices, list(BACKENDS))
        correct = sum(
            1
            for c in categories
            if fitted.get(c, [None])[:1] == full_table.get(c, [None])[:1]
        )
        curve[n] = correct
        emit(f"| {n} | {n * len(BACKENDS)} | {correct}/{len(categories)} |")
    emit()
    emit(
        "This is the number worth arguing about. If the table only stabilises after "
        "hundreds of paired runs, a headroom heuristic is the right default until "
        "someone pays for the calibration."
    )
    emit()

    if stamp == "SYNTHETIC":
        emit(
            "> SYNTHETIC. Every number above is replayed from "
            f"`{Path(args.trace).name}`, generated by `crewbench/simulate.py` from "
            "invented priors. The ranking of backends and of policies is a property "
            "of those priors, not evidence about Claude Code or Codex. Swap in real "
            "observations with `--runner crew`."
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")
    (out_dir / "results.json").write_text(
        json.dumps(
            {
                "stamp": stamp,
                "runner": runner.kind,
                "tasks": len(tasks),
                "seed": args.seed,
                "prices": asdict(prices),
                "per_backend": {b: asdict(summaries[b]) for b in BACKENDS},
                "measured_table": table,
                "full_trace_table": full_table,
                "calibration_curve": curve,
                "replay": replay_rows,
                "anchors": {
                    "blended_dollars_per_million": ANCHOR_BLENDED_DOLLARS_PER_MILLION,
                    "tokens_per_pr": ANCHOR_TOKENS_PER_PR,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"\nwrote {out_dir / 'report.md'} and {out_dir / 'results.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="crewbench")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("gen-trace", help="regenerate the SYNTHETIC default trace")
    gen.add_argument("--tasks", type=int, default=200)
    gen.add_argument("--seed", type=int, default=7)
    gen.add_argument("--out", default=str(DEFAULT_TRACE))
    gen.set_defaults(func=cmd_gen_trace)

    run = sub.add_parser("run", help="benchmark + counterfactual router replay")
    run.add_argument("--runner", default="replay", choices=("replay", "crew"))
    run.add_argument("--trace", default=str(DEFAULT_TRACE))
    run.add_argument("--prices", default=str(DEFAULT_PRICES))
    run.add_argument("--tasks", type=int, default=200)
    run.add_argument("--seed", type=int, default=7)
    run.add_argument("--default-agent", default="claude", help="groundcrew agents.default")
    run.add_argument("--calibration-fraction", type=float, default=0.25)
    run.add_argument("--out-dir", default=str(ROOT / "out"))
    run.add_argument("--session-capacity", type=int, default=PlanLimits().session_capacity_tokens)
    run.add_argument("--weekly-capacity", type=int, default=PlanLimits().weekly_capacity_tokens)
    run.add_argument("--source", default="linear", help="crew runner only: task source name")
    run.add_argument("--token-probe", default=None, help="crew runner only: token JSON command")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
