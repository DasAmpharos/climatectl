import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import anthropic
import httpx2
import pytest

from climatectl import store
from climatectl.envfile import ConfigError
from climatectl.planner import claude, cli, config, context, guardrails, house, runs

ZONE = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 13, 17, 0, tzinfo=UTC)  # Friday 12:00 local
EXAMPLE = Path(__file__).parent.parent / "planner.example.yaml"


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE)


@pytest.fixture
def conn(tmp_path):
    conn = store.connect(tmp_path / "climatectl.db", ZONE)
    runs.prepare(conn)
    return conn


def snapshot(conn, at, temp, device="up", name="Upstairs", hvac="idle", **kw):
    row = {
        "online": 1, "mode": "heat_cool", "eco": 0, "humidity_pct": 50.0,
        "heat_c": config.to_c(64, "F"), "cool_c": config.to_c(72, "F"), **kw,
    }  # fmt: skip
    conn.execute(
        "INSERT OR REPLACE INTO thermostat_snapshot (taken_at, device_id, name, online,"
        " ambient_c, humidity_pct, mode, hvac, eco, heat_c, cool_c)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (iso(at), device, name, row["online"], temp, row["humidity_pct"], row["mode"], hvac,
         row["eco"], row["heat_c"], row["cool_c"]),
    )  # fmt: skip


def event(conn, at, device="up", **fields):
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO thermostat_event (event_id, at, device_id, {cols}) VALUES (?, ?, ?, {marks})",
        (f"{device}-{iso(at)}-{cols}", iso(at), device, *fields.values()),
    )


def outdoor(conn, at, temp, solar=0.0):
    conn.execute(
        "INSERT OR REPLACE INTO weather_observation (observed_at, fetched_at, temp_c, solar_wm2)"
        " VALUES (?, ?, ?, ?)",
        (iso(at), iso(at), temp, solar),
    )


def cfg(**changes) -> config.Config:
    raw = {
        "unit": "F",
        "house": "Two floors.",
        "levels": {
            "Upstairs": {
                "about": "Bedroom.",
                "bands": [
                    {"name": "day", "heat": 64, "cool": 72},
                    {"name": "sleep", "from": "22:00", "to": "07:00", "heat": 62, "cool": 69},
                ],
            }
        },
        **changes,
    }
    return config.parse(raw, Path("."))


def day_only(**changes) -> config.Config:
    return cfg(levels={"Upstairs": {"bands": [{"name": "day", "heat": 64, "cool": 72}]}},
               **changes)  # fmt: skip


def ctx_for(c: config.Config, mode="heat_cool", heat_f=64, cool_f=72, skip=None, now=NOW):
    state = context.LevelState(
        c.levels[0], "up", mode, config.to_c(heat_f, "F"), config.to_c(cool_f, "F"), skip
    )
    return context.Context(now, ZONE, {}, {"Upstairs": state})


def f(celsius):
    return None if celsius is None else round(config.from_c(celsius, "F"), 1)


def changes(plan, level="Upstairs"):
    lp = next(lp for lp in plan.levels if lp.level == level)
    return [(c.at.astimezone(ZONE).strftime("%a %H:%M"), f(c.heat_c), f(c.cool_c))
            for c in lp.changes]  # fmt: skip


# --- config ---


def test_example_config_parses():
    c = config.load(EXAMPLE)
    assert [lv.key for lv in c.levels] == ["Upstairs", "Downstairs"]
    up = c.levels[0]
    assert up.band_at(time(23, 0)).name == "sleep"
    assert up.band_at(time(6, 59)).name == "sleep"  # the band crosses midnight
    assert up.band_at(time(7, 0)).name == "day"
    assert round(c.limits.min_gap_c, 3) == round(3 * 5 / 9, 3)
    assert c.model == config.DEFAULT_MODEL


def test_unquoted_yaml_times_are_read_as_times(tmp_path):
    # YAML 1.1 reads 22:00 as a base-60 number (1320); it must still mean 22:00.
    path = tmp_path / "planner.yaml"
    path.write_text(
        "levels:\n  Up:\n    bands:\n      - {heat: 64, cool: 72}\n"
        "      - {from: 22:00, to: 23:30, heat: 62, cool: 69}\n"
    )
    band = config.load(path).levels[0].bands[1]
    assert (band.start, band.end) == (time(22, 0), time(23, 30))


@pytest.mark.parametrize(
    ("levels", "message"),
    [
        ({"Up": {"bands": [{"heat": 64, "cool": 72}, {"heat": 62, "cool": 70}]}}, "exactly one"),
        ({"Up": {"bands": [{"heat": 70, "cool": 71}]}}, "min_gap"),
        ({"Up": {"bands": [{"heat": 50, "cool": 72}]}}, "floor"),
        ({"Up": {"bands": [{"heat": 64, "cool": 72, "from": "22:00"}]}}, "both from and to"),
        ({"Up": {"bands": []}}, "at least one band"),
        ({}, "at least one thermostat"),
    ],
)
def test_bad_configs_are_refused(levels, message):
    with pytest.raises(ConfigError, match=message):
        config.parse({"levels": levels}, Path("."))


def test_celsius_config_gets_the_same_default_limits():
    c = config.parse(
        {"unit": "C", "levels": {"Up": {"bands": [{"heat": 18, "cool": 22}]}}}, Path(".")
    )
    assert round(c.limits.floor_c, 2) == round(config.to_c(55, "F"), 2)
    assert round(c.limits.max_step_c, 2) == round(6 * 5 / 9, 2)


# --- house model ---


def test_house_model_from_nights_and_runs(conn):
    # Ten nights cooling 20.0 -> 18.4°C with 10°C outside, the HVAC idle; snapshots hourly.
    for day in range(2, 12):
        start = local(day, 22)
        for h in range(9):
            at = start + timedelta(hours=h)
            snapshot(conn, at, 20.0 - 0.2 * h)
            outdoor(conn, at, 10.0)
    # Three 15-minute furnace runs, each 18.0 -> 19.5°C, reported 2 minutes after it stops.
    for hour in (14, 15, 16):
        at = local(12, hour)
        snapshot(conn, at, 18.0)
        event(conn, at + timedelta(seconds=1), hvac="heating")
        event(conn, at + timedelta(minutes=15), hvac="idle")
        event(conn, at + timedelta(minutes=17), temp_c=19.5)
    m = house.fit(conn, "up", NOW, ZONE)
    gap = (20.0 + 18.4) / 2 - 10.0
    assert m.loss_nights == 10
    assert m.loss_per_h == pytest.approx(0.2 / gap, rel=1e-3)
    assert m.outdoor_c_range == (10.0, 10.0)
    assert m.heat_runs == 3
    assert m.heat_c_per_h == pytest.approx(1.5 / 0.25, rel=0.01)
    assert m.cool_c_per_h is None and m.cool_runs == 0


def test_house_model_needs_enough_data(conn):
    snapshot(conn, NOW - timedelta(hours=1), 20.0)
    m = house.fit(conn, "up", NOW, ZONE)
    assert (m.loss_per_h, m.heat_c_per_h, m.cool_c_per_h, m.sun_c_per_kwh) == (None,) * 4


# --- context ---


def seed_now(conn, **kw):
    snapshot(conn, NOW - timedelta(minutes=10), config.to_c(66, "F"), **kw)
    snapshot(conn, NOW - timedelta(minutes=10), 20.0, device="down", name="Downstairs")


def test_context_payload(conn, tmp_path):
    seed_now(conn)
    issued = iso(NOW - timedelta(hours=2))
    for h in range(-3, 30):
        conn.execute(
            "INSERT INTO weather_forecast (issued_at, valid_at, temp_c, solar_wm2)"
            " VALUES (?, ?, ?, ?)",
            (issued, iso(NOW + timedelta(hours=h)), 10.0, 100.0),
        )
    feedback = tmp_path / "feedback.csv"
    feedback.write_text(
        "at_utc,level,feeling,note\n"
        "2026-10-01T02:00:00Z,upstairs,hot,too old\n"
        "2026-11-12T03:00:00Z,upstairs,hot,warm at bedtime\n"
        "2026-11-20T03:00:00Z,upstairs,cold,in the future\n"
        "not a time,upstairs,fine,\n"
    )
    notes = tmp_path / "notes.md"
    notes.write_text("- Prefers it cool at night.\n")
    c = cfg(feedback=str(feedback), notes=str(notes))
    ctx = context.build(conn, c, NOW, ZONE)
    p = ctx.payload
    assert p["now"] == "2026-11-13T12:00 Friday"
    (up,) = p["levels"]
    assert up["level"] == "Upstairs" and up["planned"] is True
    assert up["now"]["temp"] == 66.0 and up["now"]["heat_setpoint"] == 64.0
    assert [b["name"] for b in up["comfort_bands"]] == ["day", "sleep"]
    assert up["comfort_bands"][1] == {
        "name": "sleep", "from": "22:00", "to": "07:00",
        "heat_at_least": 62.0, "cool_at_most": 69.0,
    }  # fmt: skip
    assert len(p["forecast"]) == 26  # from an hour ago to 24 hours ahead
    assert p["forecast"][0]["temp"] == 50.0
    assert [r["note"] for r in p["feedback"]] == ["warm at bedtime"]
    assert p["feedback"][0]["at_local"] == "2026-11-11T22:00 Wed"
    assert p["notes"] == "- Prefers it cool at night.\n"
    text = json.dumps(p)
    for leak in ('"up"', "down", "latitude", "longitude"):
        assert leak not in text  # no device ids, no location


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"eco": 1}, "Eco is on"),
        ({"online": 0}, "offline"),
        ({"mode": "off"}, "system is off"),
    ],
)
def test_context_skips_levels_it_must_not_plan(conn, kw, reason):
    seed_now(conn, **kw)
    ctx = context.build(conn, cfg(), NOW, ZONE)
    assert reason in ctx.levels["Upstairs"].skip
    assert ctx.payload["levels"][0]["planned"] is False


def test_a_recent_change_at_the_thermostat_pauses_the_level(conn):
    snapshot(conn, NOW - timedelta(hours=3), 20.0)  # 64/72 before
    event(conn, NOW - timedelta(hours=2), heat_c=config.to_c(64, "F"), cool_c=config.to_c(69, "F"))
    seed_now(conn, cool_c=config.to_c(69, "F"))
    ctx = context.build(conn, cfg(), NOW, ZONE)
    assert "changed at the thermostat at 10:00" in ctx.levels["Upstairs"].skip


def test_context_skips_a_stale_or_unknown_level(conn):
    snapshot(conn, NOW - timedelta(hours=2), 20.0)
    c = config.parse(
        {"levels": {"Upstairs": {"bands": [{"heat": 64, "cool": 72}]},
                    "Attic": {"bands": [{"heat": 64, "cool": 72}]}}},
        Path("."),
    )  # fmt: skip
    ctx = context.build(conn, c, NOW, ZONE)
    assert ctx.levels["Upstairs"].skip == "no recent snapshot"
    assert ctx.levels["Attic"].skip == "no thermostat by this name"


# --- guardrails ---


def test_a_good_plan_passes_unchanged():
    c = cfg()
    plan = guardrails.apply(
        {"levels": [{"level": "Upstairs", "rationale": "Sleep band at 22:00, day at 07:00.",
                     "changes": [{"at": "2026-11-13T22:00", "heat": 62, "cool": 69},
                                 {"at": "2026-11-14T07:00", "heat": 64, "cool": 72}]}],
         "notes_update": ""},
        ctx_for(c), c,
    )  # fmt: skip
    assert changes(plan) == [("Fri 22:00", 62.0, 69.0), ("Sat 07:00", 64.0, 72.0)]
    assert plan.levels[0].corrections == [] and plan.corrections == []


def test_comfort_bands_are_enforced():
    c = cfg()
    plan = guardrails.apply(
        {"levels": [{"level": "Upstairs", "rationale": "",
                     # Too cold for the day band, and the night change keeps cool at 72.
                     "changes": [{"at": "2026-11-13T13:00", "heat": 60, "cool": 72},
                                 {"at": "2026-11-13T22:00", "heat": 62, "cool": 72},
                                 {"at": "2026-11-14T07:00", "heat": 64, "cool": 72}]}],
         "notes_update": ""},
        ctx_for(c), c,
    )  # fmt: skip
    # The first change spans no band change: 60 -> 64. Its heat equals the current one, so
    # it's dropped as a no-op; the night change is held to the sleep band's cool 69.
    assert changes(plan) == [("Fri 22:00", 62.0, 69.0), ("Sat 07:00", 64.0, 72.0)]
    texts = " | ".join(plan.levels[0].corrections)
    assert "heat raised 60.0F -> 64.0F" in texts and "cool lowered 72.0F -> 69.0F" in texts


def test_current_setpoints_that_break_a_coming_band_get_a_change():
    # At 21:30 with no changes planned, cool 72 would hold through the 22:00 sleep band (69).
    c = cfg(limits={"min_interval_minutes": 15})
    ctx = ctx_for(c, now=local(13, 21, 30))
    plan = guardrails.apply({"levels": [{"level": "Upstairs", "rationale": "", "changes": []}],
                             "notes_update": ""}, ctx, c)  # fmt: skip
    assert changes(plan) == [("Fri 21:30", 64.0, 69.0)]


def test_max_step_min_gap_floor_and_rounding():
    wide = {"Upstairs": {"bands": [{"name": "day", "heat": 60, "cool": 80}]}}
    c = cfg(levels=wide, limits={"max_step": 4, "min_gap": 3, "ceiling": 80})
    plan = guardrails.apply(
        {"levels": [{"level": "Upstairs", "rationale": "",
                     "changes": [{"at": "2026-11-13T13:00", "heat": 70.2, "cool": 71.1},
                                 {"at": "2026-11-13T16:00", "heat": 66, "cool": 95}]}],
         "notes_update": ""},
        ctx_for(c), c,
    )  # fmt: skip
    # 13:00: heat 70.2 is a step of 6.2 from 64 -> 68; cool 71.1 rounds down to 71, then
    # min_gap lifts it to 71 (68 + 3). 16:00: cool 95 -> 75 (step 4 from 71).
    assert changes(plan) == [("Fri 13:00", 68.0, 71.0), ("Fri 16:00", 66.0, 75.0)]
    assert any("max_step" in t for t in plan.levels[0].corrections)


def test_times_are_checked():
    c = day_only()
    plan = guardrails.apply(
        {"levels": [{"level": "Upstairs", "rationale": "",
                     "changes": [{"at": "soon", "heat": 66, "cool": 72},
                                 {"at": "2026-11-13T10:00", "heat": 65, "cool": 72},
                                 {"at": "2026-11-13T14:07", "heat": 66, "cool": 72},
                                 {"at": "2026-11-13T14:30", "heat": 67, "cool": 72},
                                 {"at": "2026-11-15T09:00", "heat": 64, "cool": 72}]}],
         "notes_update": ""},
        ctx_for(c), c,
    )  # fmt: skip
    # 10:00 is in the past, so it starts now; 14:07 rounds to 14:00; 14:30 is too soon after
    # it; Sunday is past the horizon.
    assert changes(plan) == [("Fri 12:00", 65.0, 72.0), ("Fri 14:00", 66.0, 72.0)]
    texts = " | ".join(plan.levels[0].corrections)
    assert "unreadable" in texts and "under 60 min" in texts and "horizon" in texts


def test_setpoints_on_a_band_edge_need_no_correction():
    # Nest values a hair outside the band edges: 64F just under, 72F just over.
    c = day_only()
    state = context.LevelState(c.levels[0], "up", "heat_cool", 17.77777, 22.22223, None)
    ctx = context.Context(NOW, ZONE, {}, {"Upstairs": state})
    plan = guardrails.apply({"levels": [], "notes_update": ""}, ctx, c)
    assert changes(plan) == [] and plan.levels[0].corrections == []


def test_nest_values_just_off_a_whole_degree_round_to_it():
    # Downstairs on 2026-11-16 as Nest reported it: 64.004/71.996F, against a 71 band. The fix
    # to 64/71 must be planned, not mistaken for the current setpoints rounded to 65/71.
    c = cfg(levels={"Upstairs": {"bands": [{"name": "day", "heat": 64, "cool": 71}]}})
    heat, cool = config.to_c(64.004, "F"), config.to_c(71.996, "F")
    state = context.LevelState(c.levels[0], "up", "heat_cool", heat, cool, None)
    ctx = context.Context(NOW, ZONE, {}, {"Upstairs": state})
    plan = guardrails.apply({"levels": [], "notes_update": ""}, ctx, c)
    assert changes(plan) == [("Fri 12:00", 64.0, 71.0)]


def test_a_first_change_due_soon_starts_now():
    # Downstairs on 2026-11-16: cool 72 against a 71 band, and the model's fix 30 minutes out.
    c = cfg(levels={"Upstairs": {"bands": [{"name": "day", "heat": 64, "cool": 71}]}})
    plan = guardrails.apply(
        {"levels": [{"level": "Upstairs", "rationale": "",
                     "changes": [{"at": "2026-11-13T12:30", "heat": 64, "cool": 71}]}],
         "notes_update": ""},
        ctx_for(c), c,
    )  # fmt: skip
    assert changes(plan) == [("Fri 12:00", 64.0, 71.0)]
    assert plan.levels[0].corrections == []


def test_heat_only_mode_plans_only_heat():
    c = cfg()
    plan = guardrails.apply(
        {"levels": [{"level": "Upstairs", "rationale": "",
                     "changes": [{"at": "2026-11-13T22:00", "heat": 62, "cool": 69},
                                 {"at": "2026-11-14T07:00", "heat": 64, "cool": 72}]}],
         "notes_update": ""},
        ctx_for(c, mode="heat"), c,
    )  # fmt: skip
    assert changes(plan) == [("Fri 22:00", 62.0, None), ("Sat 07:00", 64.0, None)]


def test_skipped_and_unknown_levels():
    c = cfg()
    plan = guardrails.apply(
        {"levels": [{"level": "Upstairs", "rationale": "x",
                     "changes": [{"at": "2026-11-13T22:00", "heat": 62, "cool": 69}]},
                    {"level": "Garage", "rationale": "", "changes": []}],
         "notes_update": "  - likes it cool  "},
        ctx_for(c, skip="Eco is on"), c,
    )  # fmt: skip
    (lp,) = plan.levels
    assert lp.skipped == "Eco is on" and lp.changes == []
    assert "not planned" in lp.corrections[0]
    assert plan.corrections == ["ignored a plan for unknown level 'Garage'"]
    assert plan.notes_update == "- likes it cool"


# --- the Claude call ---


def walk_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for v in node.values():
            yield from walk_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_objects(v)


def test_request_shape():
    c = cfg()
    kwargs = claude.request(ctx_for(c), c)
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["output_config"]["effort"] == "medium"
    fmt = kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    objects = list(walk_objects(fmt["schema"]))
    assert len(objects) == 3 and all(o["additionalProperties"] is False for o in objects)
    assert all(set(o["required"]) == set(o["properties"]) for o in objects)
    assert "temperature" not in kwargs  # rejected on API version 2026-10-21
    # Opus gets the server-side refusal fallback; Haiku has none.
    assert kwargs["betas"] == [claude.FALLBACK_BETA] and kwargs["fallbacks"] == "default"
    haiku = claude.request(ctx_for(c), c, "claude-haiku-5-5")
    assert "betas" not in haiku and "fallbacks" not in haiku
    system = kwargs["system"][0]["text"]
    assert "Never below 55F or above 86F, and cool at least 3F above heat." in system
    assert "- Upstairs: Bedroom." in system


def fake_client(messages):
    """A client whose plain and beta endpoints both record into `messages`."""
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def response(text='{"levels": [], "notes_update": ""}', stop="end_turn", model="claude-opus-5-5"):
    usage = SimpleNamespace(
        input_tokens=3000, output_tokens=900,
        cache_read_input_tokens=2000, cache_creation_input_tokens=0,
    )  # fmt: skip
    content = [SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop, model=model, usage=usage)


def test_call_parses_the_reply_and_estimates_cost():
    reply = claude.call(fake_client(FakeMessages(response())), {"max_tokens": 10})
    assert reply.raw == {"levels": [], "notes_update": ""}
    assert reply.cost_usd == pytest.approx((3000 * 4 + 2000 * 0.20 + 900 * 20) / 1e6)


def test_call_errors():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    status = anthropic.APIStatusError(
        "overloaded", response=httpx2.Response(529, request=request), body=None
    )
    for fake, message in [
        (FakeMessages(response(stop="refusal")), "declined"),
        (FakeMessages(response(stop="max_tokens")), "max_tokens"),
        (FakeMessages(response(text="not json")), "valid JSON"),
        (FakeMessages(error=status), "529"),
        (FakeMessages(error=anthropic.APIConnectionError(request=request)), "APIConnectionError"),
        (FakeMessages(error=anthropic.CredentialsError("none")), "ant auth login"),
    ]:
        with pytest.raises(claude.PlannerError, match=message):
            claude.call(fake_client(fake), {"max_tokens": 10})


def test_fallback_requests_use_the_beta_endpoint():
    plain, beta = FakeMessages(response()), FakeMessages(response())
    client = SimpleNamespace(messages=plain, beta=SimpleNamespace(messages=beta))
    claude.call(client, {"max_tokens": 10, "betas": [claude.FALLBACK_BETA], "fallbacks": "default"})
    claude.call(client, {"max_tokens": 10})
    assert len(beta.calls) == 1 and "fallbacks" in beta.calls[0]
    assert len(plain.calls) == 1 and "fallbacks" not in plain.calls[0]


# --- the command ---


@pytest.fixture
def house_dir(tmp_path, monkeypatch, conn):
    (tmp_path / ".env").write_text("CLIMATECTL_TIMEZONE=America/New_York\n")
    (tmp_path / "planner.yaml").write_text(EXAMPLE.read_text())
    monkeypatch.setenv("CLIMATECTL_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("CLIMATECTL_DB", str(tmp_path / "climatectl.db"))
    for var in ("PLANNER_CONFIG", "ANTHROPIC_API_KEY", "CLIMATECTL_TIMEZONE"):
        monkeypatch.delenv(var, raising=False)
    now = datetime.now(UTC)
    snapshot(conn, now - timedelta(minutes=5), 19.0)
    conn.commit()
    return tmp_path


def test_input_only_needs_no_key_and_sends_nothing(house_dir, capsys, monkeypatch):
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: pytest.fail("no client expected"))
    cli.main(["--input-only"])
    out = capsys.readouterr().out
    assert "You plan thermostat setpoints" in out and "This run's input:" in out


def test_plan_without_credentials_stops(house_dir, monkeypatch, conn):
    def no_credentials(**kw):
        raise anthropic.CredentialsError("no credentials found")

    monkeypatch.setattr(anthropic, "Anthropic", no_credentials)
    with pytest.raises(SystemExit, match="ant auth login"):
        cli.main([])
    assert "ant auth login" in conn.execute("SELECT error FROM plan_run").fetchone()[0]


def test_without_a_key_the_sdk_finds_the_credentials(house_dir, capsys, monkeypatch):
    calls = []

    def client(**kw):
        calls.append(kw)
        return fake_client(FakeMessages(response()))

    monkeypatch.setattr(anthropic, "Anthropic", client)
    cli.main([])
    assert calls == [{}]  # no api_key: the SDK's own lookup, e.g. an `ant auth login` profile


def test_plan_stores_the_run(house_dir, capsys, monkeypatch, conn):
    with (house_dir / ".env").open("a") as env:
        env.write("ANTHROPIC_API_KEY=sk-test-not-real\n")
    reply = json.dumps({
        "levels": [{"level": "Upstairs", "rationale": "Hold the day band.", "changes": []}],
        "notes_update": "",
    })  # fmt: skip
    fake = FakeMessages(response(text=reply))
    keys = []

    def client(api_key):
        keys.append(api_key)
        return fake_client(fake)

    monkeypatch.setattr(anthropic, "Anthropic", client)
    cli.main([])
    out = capsys.readouterr().out
    assert keys == ["sk-test-not-real"]
    assert "Upstairs" in out and "why: Hold the day band." in out and "sk-test" not in out
    assert "Downstairs\n  not planned: no thermostat by this name" in out
    assert "simulated 24 h" in out and "hold the setpoints" in out
    assert conn.execute("SELECT score_json FROM plan_run").fetchone()[0] is not None
    row = conn.execute("SELECT model, error, plan_json, cost_usd FROM plan_run").fetchone()
    assert row[0] == "claude-opus-5-5" and row[1] is None and row[3] > 0
    assert json.loads(row[2])[0]["level"] == "Upstairs"


def test_a_failed_call_is_stored_with_its_error(house_dir, monkeypatch, conn):
    with (house_dir / ".env").open("a") as env:
        env.write("ANTHROPIC_API_KEY=sk-test-not-real\n")
    fake = FakeMessages(response(stop="refusal"))
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: fake_client(fake))
    with pytest.raises(SystemExit, match="declined"):
        cli.main([])
    assert conn.execute("SELECT error FROM plan_run").fetchone()[0].startswith("Claude declined")
