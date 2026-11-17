"""Every planner run, kept in `plan_run`: the input, the model's reply, the plan after the
guardrails, their corrections, the tokens and an estimated cost. A failed run is kept too, with
its error, so gaps in planning are visible."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from typing import Any

from .claude import Reply
from .guardrails import Plan

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_run (
    id                 INTEGER PRIMARY KEY,
    created_at         TEXT NOT NULL,
    model              TEXT NOT NULL,   -- the model that answered (or was asked, on an error)
    input_json         TEXT NOT NULL,   -- what Claude was sent (the user message)
    reply_json         TEXT,            -- the parsed reply
    plan_json          TEXT,            -- per level: changes (UTC, Celsius), skip reason, rationale
    corrections_json   TEXT,            -- every guardrail fix, by level ("" for the whole plan)
    notes_update       TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_usd           REAL,            -- an estimate from list prices
    stop_reason        TEXT,
    error              TEXT,
    applied            INTEGER NOT NULL DEFAULT 0,  -- 0 until the planner writes setpoints
    score_json         TEXT             -- simulate.py's scores: the plan and the baselines
);
CREATE INDEX IF NOT EXISTS plan_run_time ON plan_run (created_at);
"""


def prepare(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_run)")}
    if "score_json" not in columns:  # tables from before scoring
        conn.execute("ALTER TABLE plan_run ADD COLUMN score_json TEXT")
        conn.commit()


def save_score(conn: sqlite3.Connection, run_id: int, score: dict[str, Any]) -> None:
    conn.execute("UPDATE plan_run SET score_json = ? WHERE id = ?", (json.dumps(score), run_id))
    conn.commit()


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_dict(plan: Plan) -> list[dict[str, Any]]:
    return [
        {
            "level": lp.level,
            "skipped": lp.skipped,
            "rationale": lp.rationale,
            "changes": [{**asdict(c), "at": _stamp(c.at)} for c in lp.changes],
        }
        for lp in plan.levels
    ]


def corrections(plan: Plan) -> dict[str, list[str]]:
    out = {lp.level: lp.corrections for lp in plan.levels if lp.corrections}
    if plan.corrections:
        out[""] = plan.corrections
    return out


def save(
    conn: sqlite3.Connection,
    now: datetime,
    model: str,
    payload: dict[str, Any],
    reply: Reply | None = None,
    plan: Plan | None = None,
    error: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO plan_run (created_at, model, input_json, reply_json, plan_json,"
        " corrections_json, notes_update, input_tokens, output_tokens, cache_read_tokens,"
        " cache_write_tokens, cost_usd, stop_reason, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _stamp(now),
            reply.model if reply else model,
            json.dumps(payload),
            json.dumps(reply.raw) if reply else None,
            json.dumps(plan_dict(plan)) if plan else None,
            json.dumps(corrections(plan)) if plan else None,
            plan.notes_update if plan else None,
            reply and reply.input_tokens,
            reply and reply.output_tokens,
            reply and reply.cache_read_tokens,
            reply and reply.cache_write_tokens,
            reply and reply.cost_usd,
            reply and reply.stop_reason,
            error,
        ),
    )
    conn.commit()
    return cur.lastrowid
