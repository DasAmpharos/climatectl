"""`climatectl plan`: a 24-hour setpoint plan from Claude (a dry run: never applied).

The plan is checked by the guardrails, scored by the simulator against the no-AI baselines
(see `climatectl score`) and stored in plan_run; nothing is written to a thermostat.

Needs planner.yaml (PLANNER_CONFIG to put it elsewhere; see planner.example.yaml) and
Anthropic credentials: ANTHROPIC_API_KEY if set, else whatever the SDK finds, such as a profile
from `ant auth login`. Every call costs money; --input-only prints what would be sent, for
free."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import UTC, datetime

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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="climatectl plan", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input-only",
        action="store_true",
        help="print the request that would be sent and exit (no API call)",
    )
    parser.add_argument("--model", help="override the config's model for this run")
    parser.add_argument("--effort", choices=EFFORTS, help="override the config's effort")
    parser.add_argument("--json", action="store_true", help="print the stored plan as JSON")
    args = parser.parse_args(argv)  # fmt: skip

    try:
        cfg = config.load(config.path_from_settings())
        zone = store.home_timezone()
    except ConfigError as e:
        sys.exit(str(e))
    if args.effort:
        cfg = dataclasses.replace(cfg, effort=args.effort)
    conn = store.connect(store.db_path(), zone)
    runs.prepare(conn)
    now = datetime.now(UTC)
    ctx = context.build(conn, cfg, now, zone)
    kwargs = claude.request(ctx, cfg, args.model)

    if args.input_only:
        print(kwargs["system"][0]["text"])
        print(kwargs["messages"][0]["content"])
        return

    # A key from .env or the environment; without one, the SDK's own lookup (an `ant auth
    # login` profile, among others).
    key = envfile.read_settings(("ANTHROPIC_API_KEY",)).get("ANTHROPIC_API_KEY")
    try:
        try:
            client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        except anthropic.CredentialsError:
            raise claude.PlannerError(claude.NO_CREDENTIALS) from None
        reply = claude.call(client, kwargs)
    except claude.PlannerError as e:
        runs.save(conn, now, kwargs["model"], ctx.payload, error=str(e))
        sys.exit(str(e))
    plan = guardrails.apply(reply.raw, ctx, cfg)
    run_id = runs.save(conn, now, kwargs["model"], ctx.payload, reply, plan)
    scores = score_cli.evaluate(ctx.payload, runs.plan_dict(plan), now, zone)
    runs.save_score(conn, run_id, score_cli.to_json(scores))
    if args.json:
        print(json.dumps({"run": run_id, "plan": runs.plan_dict(plan),
                          "corrections": runs.corrections(plan)}, indent=1))  # fmt: skip
    else:
        print(render(plan, reply, ctx, cfg.unit))
        print(score_cli.render(scores, cfg.unit, zone))


if __name__ == "__main__":
    main()
