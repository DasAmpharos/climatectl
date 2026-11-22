import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_planner import EXAMPLE, ZONE, event, iso, outdoor, snapshot

from climatectl import store
from climatectl.envfile import ConfigError
from climatectl.planner import config, runs, scorecard, scorecard_cli

D = date.fromisoformat


def trial_cfg(**trial) -> config.Config:
    raw = {
        "unit": "F",
        "levels": {"Upstairs": {"bands": [
            {"name": "day", "heat": 64, "cool": 72},
            {"name": "sleep", "from": "21:00", "to": "07:00", "heat": 62, "cool": 68},
        ]}},
        "trial": {"start": "2026-11-11", "end": "2026-11-20", "hold": {"heat": 64, "cool": 72},
                  "levels": {"Upstairs": "2026-11-11"}, "baseline_from": "2026-11-01",
                  **trial},
    }  # fmt: skip
    return config.parse(raw, Path("."))


def test_the_example_trial_parses():
    t = config.load(EXAMPLE).trial
    assert t is not None and t.day_starts.hour == 21


def test_arms_alternate_from_the_start_on_every_level():
    raw = trial_cfg().trial
    t = config.Trial(raw.start, raw.end, raw.day_starts, raw.hold_heat_c, raw.hold_cool_c,
                     {"Upstairs": D("2026-11-11"), "Downstairs": D("2026-11-14")},
                     raw.baseline_from, frozenset({D("2026-11-16")}), raw.tolerance_c)  # fmt: skip
    up = [t.arm("Upstairs", D("2026-11-10") + timedelta(days=i)) for i in range(12)]
    assert up == ["baseline", "planned", "hold", "planned", "hold", "planned", "skipped",
                  "planned", "hold", "planned", "hold", "after"]  # fmt: skip
    # A later level joins on the same arm the others are on that day.
    assert [t.arm("Downstairs", D(f"2026-11-{d}")) for d in (13, 14, 15)] == [
        "baseline", "hold", "planned"]  # fmt: skip
    assert t.arm("Nowhere", D("2026-11-12")) == "baseline"


@pytest.mark.parametrize(
    "trial, message",
    [
        ({"end": "2026-11-01"}, "end must not be before"),
        ({"levels": {"Attic": "2026-11-11"}}, "isn't one of the levels"),
        ({"levels": {"Upstairs": "2026-12-01"}}, "between trial.start and trial.end"),
        ({"hold": {"heat": 70, "cool": 71}}, "min_gap"),
        ({"start": "soon"}, "must be a date"),
    ],
)
def test_bad_trials_are_refused(trial, message):
    with pytest.raises(ConfigError, match=message):
        trial_cfg(**trial)


# --- the scorecard, on synthetic days ---
# Each trial day (21:00 to 21:00) has one outdoor temperature. A hold day runs the furnace
# 20 min per °C-day below 12 °C, in one run at 03:00; a planned day runs 15% less.

BASE_C, SLOPE = 12.0, 20.0


def furnace_for(temp_c: float, planned: bool) -> float:
    need = SLOPE * max(0.0, BASE_C - temp_c)
    return need * (0.85 if planned else 1.0)


def house(conn, cfg, days: dict[str, float], off_hold: str | None = None, gap: str | None = None):
    """Days by start date -> outdoor °C. Wobbles the minutes a little so the spread isn't 0."""
    t = cfg.trial
    for i, (day, temp) in enumerate(days.items()):
        start = datetime.combine(D(day), t.day_starts, tzinfo=ZONE)
        for h in range(24):
            at = start + timedelta(hours=h)
            if day == gap and 3 <= h < 12:
                # The logger was down; only the forecast has these hours.
                conn.execute(
                    "INSERT INTO weather_forecast (issued_at, valid_at, temp_c) VALUES (?, ?, ?)",
                    (iso(at - timedelta(hours=6)), iso(at), temp),
                )
                continue
            outdoor(conn, at, temp)
        for h in (0, 12):  # snapshots: readings, the name and the setpoints
            heat = 68.0 if day == off_hold else 64.0
            snapshot(conn, start + timedelta(hours=h), 19.0, heat_c=config.to_c(heat, "F"))
        minutes = furnace_for(temp, t.arm("Upstairs", D(day)) == "planned") + (i % 3 - 1) * 2
        if minutes > 0:
            run = start + timedelta(hours=6)
            event(conn, run, hvac="heating")
            event(conn, run + timedelta(minutes=minutes), hvac="idle")
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    conn = store.connect(tmp_path / "climatectl.db", ZONE)
    runs.prepare(conn)
    return conn


TEMPS = [2.0, 4.0, 6.0, 3.0, 8.0, 5.0, 1.0, 7.0, 9.0, 3.5]  # °C, repeated


def all_days(first="2026-11-01", count=20):
    start = D(first)
    return {(start + timedelta(days=i)).isoformat(): TEMPS[i % len(TEMPS)] for i in range(count)}


def test_the_fit_finds_the_weather_response_and_the_saving(conn):
    cfg = trial_cfg()
    house(conn, cfg, all_days())
    (card,) = scorecard.build(conn, cfg, ZONE, datetime(2026, 11, 22, 12, tzinfo=UTC))
    assert len(card.days) == 20
    assert card.fit.base_c == pytest.approx(BASE_C, abs=0.6)
    assert card.fit.slope == pytest.approx(SLOPE, rel=0.1) and card.fit.r2 > 0.98
    e = card.effect
    assert (e.planned, e.hold) == (5, 5)
    assert e.saved_pct == pytest.approx(15, abs=3)
    assert e.saved_min - e.interval_min > 0  # detected
    text = scorecard.render([card], cfg, ZONE)
    assert "planned vs hold (5 vs 5 days)" in text and ": a real saving" in text


def test_too_few_days_for_an_interval_says_so_without_a_confidence_level(conn):
    cfg = trial_cfg()
    house(conn, cfg, all_days())
    (card,) = scorecard.build(conn, cfg, ZONE, datetime(2026, 11, 22, 12, tzinfo=UTC))
    card.effect = scorecard.Effect(1, 1, 31.0, None, 32.0, None)
    text = scorecard.render([card], cfg, ZONE)
    assert "saves 31 min a day (32%), too few days for an interval\n" in text
    assert "95%" not in text


def test_before_the_trial_it_says_what_the_trial_could_detect(conn):
    cfg = trial_cfg()
    house(conn, cfg, all_days(count=10))
    (card,) = scorecard.build(conn, cfg, ZONE, datetime(2026, 11, 11, 23, tzinfo=UTC))
    assert card.effect is None and card.detectable_min > 0
    assert "the trial can detect a saving of about" in scorecard.render([card], cfg, ZONE)


def test_unusable_days_are_left_out_with_a_reason(conn):
    cfg = trial_cfg()
    house(conn, cfg, all_days(count=10), off_hold="2026-11-03", gap="2026-11-05")
    (card,) = scorecard.build(conn, cfg, ZONE, datetime(2026, 11, 12, 12, tzinfo=UTC))
    by_day = {d.day.isoformat(): d for d in card.days}
    assert not by_day["2026-11-03"].used
    assert "away from the hold heat setpoint" in by_day["2026-11-03"].notes[0]
    # The logger's gap is filled from the forecast, so the day still counts.
    assert by_day["2026-11-05"].used and len(by_day["2026-11-05"].hourly_c) == 24
    assert card.fit.n == 9


def test_the_day_still_running_is_left_for_later(conn):
    cfg = trial_cfg()
    house(conn, cfg, all_days(count=3))
    (card,) = scorecard.build(conn, cfg, ZONE, datetime(2026, 11, 3, 20, tzinfo=UTC))
    assert [d.day.isoformat() for d in card.days] == ["2026-11-01"]  # 11/02 ends 11/03 21:00


def test_outside_minutes_count_only_past_the_tolerance(conn):
    cfg = trial_cfg()
    start = datetime(2026, 11, 1, 21, tzinfo=ZONE)
    snapshot(conn, start, config.to_c(61.5, "F"))  # 0.5F under the sleep band: inside it
    # From 07:00 the day band (64) applies: 61.5 and then 62.5 are both over 1F under it.
    snapshot(conn, start + timedelta(hours=11), config.to_c(62.5, "F"))  # 08:00
    snapshot(conn, start + timedelta(hours=12), config.to_c(64, "F"))  # 09:00
    snapshot(conn, start + timedelta(hours=24), config.to_c(64, "F"))
    conn.commit()
    (card,) = scorecard.build(conn, cfg, ZONE, datetime(2026, 11, 3, 12, tzinfo=UTC))
    day = card.days[0]
    assert day.outside_min == 120 and round(config.from_c(day.low_c, "F"), 1) == 61.5


def test_the_simulator_is_checked_against_hold_days(conn):
    cfg = trial_cfg()
    house(conn, cfg, all_days(count=10))
    created = datetime(2026, 11, 2, 21, tzinfo=ZONE).astimezone(UTC)
    score = {"hold the setpoints": {"Upstairs": {"heat_minutes": 100.0}}}
    for model in ("a", "b"):  # one batch, counted once
        conn.execute(
            "INSERT INTO plan_run (created_at, model, input_json, score_json) VALUES (?, ?, ?, ?)",
            (iso(created), model, json.dumps({"levels": [{"level": "Upstairs"}]}),
             json.dumps(score)),
        )  # fmt: skip
    conn.commit()
    (card,) = scorecard.build(conn, cfg, ZONE, datetime(2026, 11, 11, 23, tzinfo=UTC))
    ((at, predicted, actual),) = card.simulator
    assert at == created and predicted == 100
    assert actual == pytest.approx(furnace_for(TEMPS[1], False) + 0, abs=3)


def test_the_command_prints_and_saves_the_days(tmp_path, monkeypatch, capsys, conn):
    (tmp_path / ".env").write_text("CLIMATECTL_TIMEZONE=America/New_York\n")
    (tmp_path / "planner.yaml").write_text(
        "unit: F\nlevels:\n  Upstairs:\n    bands:\n      - {name: day, heat: 64, cool: 72}\n"
        "trial:\n  start: 2026-11-11\n  end: 2026-11-20\n  hold: {heat: 64, cool: 72}\n"
        "  levels: {Upstairs: 2026-11-11}\n  baseline_from: 2026-11-01\n"
    )
    monkeypatch.setenv("CLIMATECTL_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("CLIMATECTL_DB", str(tmp_path / "climatectl.db"))
    monkeypatch.delenv("PLANNER_CONFIG", raising=False)
    house(conn, trial_cfg(), all_days(count=8))
    scorecard_cli.main([])
    out = capsys.readouterr().out
    assert "Upstairs (in the trial from Wed 11/11)" in out and "weather fit (8 days" in out
    # Days after the last reading aren't listed.
    saved = conn.execute("SELECT count(*), sum(used) FROM scorecard_day").fetchone()
    assert saved == (8, 8)
    scorecard_cli.main(["--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["level"] == "Upstairs" and len(data[0]["days"]) == 8


def test_the_command_needs_a_trial(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CLIMATECTL_TIMEZONE=America/New_York\n")
    (tmp_path / "planner.yaml").write_text(
        "unit: F\nlevels:\n  Upstairs:\n    bands:\n      - {name: day, heat: 64, cool: 72}\n"
    )
    monkeypatch.setenv("CLIMATECTL_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("CLIMATECTL_DB", str(tmp_path / "climatectl.db"))
    monkeypatch.delenv("PLANNER_CONFIG", raising=False)
    with pytest.raises(SystemExit, match="No trial"):
        scorecard_cli.main([])
