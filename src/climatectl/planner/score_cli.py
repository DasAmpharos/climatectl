"""`climatectl score`: simulate stored plans against the hold and band-edge baselines.

Each plan is run through the house model from what the planner was sent (see simulate.py), next
to two baselines: holding the setpoints it started with all day (the no-AI baseline), and
setting each band's edges as it starts, with no pre-heating or pre-cooling. `climatectl plan`
scores every new plan the same way. Changes nothing but plan_run.score_json."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .. import store
from ..envfile import ConfigError
from ..store import parse_utc
from . import runs, simulate

PLAN = "this plan"
NAMES = {"hold": "hold the setpoints", "band edges": "band edges, no pre-heat"}


def evaluate(
    payload: dict[str, Any], plan: list[dict[str, Any]], now: datetime, zone: ZoneInfo
) -> dict[str, dict[str, simulate.Score]]:
    """The plan's scores and the baselines', by name, then by level."""
    results = {PLAN: simulate.score(payload, plan, now, zone)}
    for name, baseline in simulate.baselines(payload, now, zone).items():
        results[NAMES[name]] = simulate.score(payload, baseline, now, zone)
    return results


def to_json(results: dict[str, dict[str, simulate.Score]]) -> dict[str, Any]:
    def one(s: simulate.Score) -> dict[str, Any]:
        out = asdict(s)
        out["worst_at"] = s.worst_at and s.worst_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return out

    return {name: {lv: one(s) for lv, s in scores.items()} for name, scores in results.items()}


def render(results: dict[str, dict[str, simulate.Score]], unit: str, zone: ZoneInfo) -> str:
    def t(c: float | None) -> str:
        return "?" if c is None else f"{c if unit == 'C' else c * 9 / 5 + 32:.1f}"

    lines = [
        "simulated 24 h (house model; for comparing plans, not exact)",
        f"  {'':24} {'level':11} {'outside':>8} {'furnace':>8} {'AC':>7}  {'range':12} worst miss",
    ]
    for name, scores in results.items():
        total_out = total_heat = total_cool = 0.0
        for level, s in scores.items():
            worst = (
                f"{s.worst_miss:.1f}{unit} at {s.worst_at.astimezone(zone):%a %H:%M}"
                if s.worst_at
                else "-"
            )
            lines.append(
                f"  {name:24} {level:11} {s.outside_minutes:4.0f} min {s.heat_minutes:4.0f} min"
                f" {s.cool_minutes:3.0f} min  {t(s.low_c)}-{t(s.high_c)}{unit:<3} {worst}"
            )
            total_out += s.outside_minutes
            total_heat += s.heat_minutes
            total_cool += s.cool_minutes
        lines.append(
            f"  {'':24} {'all':11} {total_out:4.0f} min {total_heat:4.0f} min {total_cool:3.0f} min"
        )
    notes = sorted({n for scores in results.values() for s in scores.values() for n in s.notes})
    lines.extend(f"  note: {n}" for n in notes)
    return "\n".join(lines)


def score_run(conn, run_id: int, zone: ZoneInfo) -> tuple[dict[str, Any], dict] | None:
    row = conn.execute(
        "SELECT created_at, input_json, plan_json FROM plan_run WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None or row[2] is None:
        return None
    payload = json.loads(row[1])
    results = evaluate(payload, json.loads(row[2]), parse_utc(row[0]), zone)
    runs.save_score(conn, run_id, to_json(results))
    return payload, results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="climatectl score", description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="*", type=int, help="plan_run ids (default: the latest)")
    args = parser.parse_args(argv)
    try:
        zone = store.home_timezone()
    except ConfigError as e:
        sys.exit(str(e))
    conn = store.connect(store.db_path(), zone)
    runs.prepare(conn)
    ids = args.runs or [
        r[0]
        for r in conn.execute(
            "SELECT id FROM plan_run WHERE plan_json IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
    ]
    if not ids:
        sys.exit("No stored plans yet; run `climatectl plan` first.")
    for run_id in ids:
        model, created = conn.execute(
            "SELECT model, created_at FROM plan_run WHERE id = ?", (run_id,)
        ).fetchone() or (None, None)
        scored = score_run(conn, run_id, zone)
        if scored is None:
            print(f"run {run_id}: no plan to score")
            continue
        payload, results = scored
        when = parse_utc(created).astimezone(zone).strftime("%a %m/%d %H:%M")
        print(f"run {run_id}: {model}, {when}")
        print(render(results, payload["unit"], zone))


if __name__ == "__main__":
    main()
