"""`climatectl plan`: a 24-hour setpoint plan from Claude (a dry run; `apply` runs a trial).

The plan is checked by the guardrails, scored by the simulator against the no-AI baselines
(see `climatectl score`) and stored in plan_run; nothing is written to a thermostat.

Several --model options plan from the same input, one run each, so their plans compare
fairly; the report starts with a table of every plan's simulated minutes next to the
baselines'. It exits non-zero if any model failed, after printing the others.

Needs planner.yaml (PLANNER_CONFIG to put it elsewhere; see planner.example.yaml) and
Anthropic credentials: ANTHROPIC_API_KEY if set, else whatever the SDK finds, such as a profile
from `ant auth login`. Every call costs money; --input-only prints what would be sent, for
free."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import anthropic

from .. import envfile, store
from ..envfile import ConfigError
from . import claude, config, context, guardrails, runs, score_cli
from .config import EFFORTS, from_c


def render(plan: guardrails.Plan, reply: claude.Reply, ctx: context.Context, unit: str) -> str:
    lines = []
    for lp in plan.levels:
        lines.append(lp.level)
        if lp.skipped:
            lines.append(f"  not planned: {lp.skipped}")
        elif not lp.changes:
            lines.append("  no changes: keeps the current setpoints")
        for c in lp.changes:
            when = c.at.astimezone(ctx.zone).strftime("%a %H:%M")
            heat = "-" if c.heat_c is None else f"{from_c(c.heat_c, unit):g}{unit}"
            cool = "-" if c.cool_c is None else f"{from_c(c.cool_c, unit):g}{unit}"
            lines.append(f"  {when}  heat {heat:>5}  cool {cool:>5}")
        if lp.rationale:
            lines.append(f"  why: {lp.rationale}")
        lines.extend(f"  correction: {text}" for text in lp.corrections)
    lines.extend(f"correction: {text}" for text in plan.corrections)
    if plan.notes_update:
        lines.append("suggested notes (not written in a dry run):")
        lines.extend(f"  {line}" for line in plan.notes_update.splitlines())
    cost = f", about ${reply.cost_usd:.4f}" if reply.cost_usd is not None else ""
    lines.append(
        f"{reply.model}: {reply.input_tokens} in + {reply.cache_read_tokens} cached"
        f" + {reply.cache_write_tokens} cache write, {reply.output_tokens} out{cost}"
    )
    return "\n".join(lines)


@dataclass
class Outcome:
    model: str
    text: str  # the plan and its scores, or the error
    data: dict | None = None  # for --json
    error: str | None = None
    totals: tuple[float, float, float] | None = None  # outside, furnace, AC minutes
    baselines: dict[str, tuple[float, float, float]] | None = None
    cost: float | None = None


def _totals(scores: dict) -> tuple[float, float, float]:
    return (
        sum(s.outside_minutes for s in scores.values()),
        sum(s.heat_minutes for s in scores.values()),
        sum(s.cool_minutes for s in scores.values()),
    )


def _client() -> anthropic.Anthropic:
    # A key from .env or the environment; without one, the SDK's own lookup (an `ant auth
    # login` profile, among others).
    key = envfile.read_settings(("ANTHROPIC_API_KEY",)).get("ANTHROPIC_API_KEY")
    try:
        return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    except anthropic.CredentialsError:
        raise claude.PlannerError(claude.NO_CREDENTIALS) from None


def plan_one(conn, client, ctx, cfg, model, now, zone) -> Outcome:
    """One model's plan, stored and scored. `client` is a PlannerError when there are no
    credentials, so the run is stored as failed like any other error."""
    kwargs = claude.request(ctx, cfg, model)
    model = kwargs["model"]
    try:
        if isinstance(client, claude.PlannerError):
            raise client
        reply = claude.call(client, kwargs)
    except claude.PlannerError as e:
        runs.save(conn, now, model, ctx.payload, error=str(e))
        return Outcome(model, f"{model}: failed: {e}", {"model": model, "error": str(e)}, str(e))
    plan = guardrails.apply(reply.raw, ctx, cfg)
    run_id = runs.save(conn, now, model, ctx.payload, reply, plan)
    scores = score_cli.evaluate(ctx.payload, runs.plan_dict(plan), now, zone)
    runs.save_score(conn, run_id, score_cli.to_json(scores))
    data = {"run": run_id, "model": reply.model, "plan": runs.plan_dict(plan),
            "corrections": runs.corrections(plan)}  # fmt: skip
    text = f"run {run_id}\n{render(plan, reply, ctx, cfg.unit)}\n"
    text += score_cli.render(scores, cfg.unit, zone)
    baselines = {name: _totals(s) for name, s in scores.items() if name != score_cli.PLAN}
    return Outcome(reply.model, text, data, totals=_totals(scores[score_cli.PLAN]),
                   baselines=baselines, cost=reply.cost_usd)  # fmt: skip


def summary(outcomes: list[Outcome]) -> str:
    """One line per model and per baseline: simulated minutes over 24 h, all levels."""
    lines = [f"  {'':26} {'outside':>8} {'furnace':>8} {'AC':>7} {'cost':>8}"]

    def row(name, totals, cost=""):
        out, heat, cool = totals
        lines.append(f"  {name:26} {out:4.0f} min {heat:4.0f} min {cool:3.0f} min {cost:>8}")

    for o in outcomes:
        if o.error:
            lines.append(f"  {o.model:26} failed: {o.error}")
        else:
            row(o.model, o.totals, "" if o.cost is None else f"${o.cost:.3f}")
    base = next((o.baselines for o in outcomes if o.baselines), {})
    for name, totals in base.items():
        row(name, totals)
    return "\n".join(lines)


def cycle(models, effort, as_json=False) -> tuple[str, list[Outcome], datetime, ZoneInfo]:
    """Plans once with every model from the same input. Raises ConfigError on bad settings."""
    cfg = config.load(config.path_from_settings())
    zone = store.home_timezone()
    if effort:
        cfg = dataclasses.replace(cfg, effort=effort)
    conn = store.connect(store.db_path(), zone)
    try:
        runs.prepare(conn)
        now = datetime.now(UTC)
        ctx = context.build(conn, cfg, now, zone)
        try:
            client = _client()
        except claude.PlannerError as e:
            client = e  # each model's run is stored with it
        outcomes = [plan_one(conn, client, ctx, cfg, m, now, zone)
                    for m in models or [cfg.model]]  # fmt: skip
    finally:
        conn.close()
    if as_json:
        return json.dumps([o.data for o in outcomes], indent=1), outcomes, now, zone
    when = now.astimezone(zone).strftime("%a %m/%d %H:%M")
    parts = [f"Plans for the 24 hours from {when} (a dry run: nothing was sent to the thermostats)",
             "", "simulated 24 h, all levels", summary(outcomes)]  # fmt: skip
    for o in outcomes:
        parts += ["", "=" * 72, o.text]
    return "\n".join(parts), outcomes, now, zone


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="climatectl plan", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input-only",
        action="store_true",
        help="print the request that would be sent and exit (no API call)",
    )
    parser.add_argument(
        "--model",
        action="append",
        help="a model to plan with instead of the config's; repeat for several",
    )
    parser.add_argument("--effort", choices=EFFORTS, help="override the config's effort")
    parser.add_argument("--json", action="store_true", help="print the stored plans as a JSON list")
    args = parser.parse_args(argv)  # fmt: skip

    try:
        if args.input_only:
            cfg = config.load(config.path_from_settings())
            zone = store.home_timezone()
            conn = store.connect(store.db_path(), zone)
            ctx = context.build(conn, cfg, datetime.now(UTC), zone)
            kwargs = claude.request(ctx, cfg, (args.model or [None])[0])
            print(kwargs["system"][0]["text"])
            print(kwargs["messages"][0]["content"])
            return
        report, outcomes, _, _ = cycle(args.model, args.effort, as_json=args.json)
    except ConfigError as e:
        sys.exit(str(e))
    print(report)
    failed = [o for o in outcomes if o.error]
    if failed:
        sys.exit("\n".join(f"{o.model}: {o.error}" for o in failed))


if __name__ == "__main__":
    main()
