import itertools
from datetime import UTC, datetime, timedelta

import pytest

from climatectl import health, store
from climatectl.envfile import ConfigError

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
_ids = itertools.count()


def iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "climatectl.db")


def snap(conn, at, ambient=20.0, hvac="idle", online=True, device="up", name="Upstairs"):
    """A snapshot; a later one at the same time replaces it."""
    conn.execute(
        "INSERT OR REPLACE INTO thermostat_snapshot"
        " (taken_at, device_id, name, online, ambient_c, hvac) VALUES (?, ?, ?, ?, ?, ?)",
        (iso(at), device, name, online, ambient, hvac),
    )


def event(conn, at, device="up", temp=None, hvac=None):
    conn.execute(
        "INSERT INTO thermostat_event (event_id, at, device_id, temp_c, hvac)"
        " VALUES (?, ?, ?, ?, ?)",
        (f"evt-{next(_ids)}", iso(at), device, temp, hvac),
    )


def every_15_min(conn, minutes_ago, until=0, **fields):
    """Snapshots from `minutes_ago` up to `until` minutes ago, as the logger takes them."""
    for m in range(minutes_ago, until - 1, -15):
        snap(conn, ago(m), **fields)


def keys(conn, events_enabled=False):
    return [f.key for f in health.check(conn, NOW, events_enabled)]


def test_nothing_to_check_on_a_fresh_install(conn):
    assert health.check(conn, NOW, events_enabled=True) == []


def test_quiet_house_raises_nothing(conn):
    every_15_min(conn, 180)
    assert keys(conn) == []


def calling(conn, status, started, before, after):
    """Idle at `before` until a call for heat or cooling `started` minutes ago (an event, then
    snapshots showing it), reading `after` at NOW."""
    every_15_min(conn, 90, until=started + 1, ambient=before)
    event(conn, ago(started), hvac=status)
    every_15_min(conn, (started - 1) // 15 * 15, ambient=before, hvac=status)
    snap(conn, NOW, ambient=after, hvac=status)


def test_heating_that_warms_the_house_is_fine(conn):
    calling(conn, "heating", 40, before=18.0, after=19.1)
    event(conn, ago(25), temp=18.5)
    assert keys(conn) == []


def test_heating_without_a_rise_alerts(conn):
    calling(conn, "heating", 40, before=18.0, after=18.1)
    (finding,) = health.check(conn, NOW, events_enabled=False)
    assert finding.key == "heat_no_rise:up"
    assert finding.title == "Upstairs: furnace may not be working"
    assert "for 40 min" in finding.message and "64.4°F to 64.6°F (+0.2°F)" in finding.message
    assert finding.title.isascii()


def test_heating_gets_a_grace_period(conn):
    calling(conn, "heating", 20, before=18.0, after=18.0)
    assert keys(conn) == []


def test_heating_that_started_before_the_lookback_window(conn):
    every_15_min(conn, 7 * 60, ambient=18.0, hvac="heating")
    (finding,) = health.check(conn, NOW, events_enabled=False)
    assert finding.key == "heat_no_rise:up" and "for 6 h" in finding.message


def test_cooling_without_a_drop_alerts(conn):
    calling(conn, "cooling", 50, before=26.0, after=25.9)
    (finding,) = health.check(conn, NOW, events_enabled=False)
    assert finding.key == "cool_no_drop:up"
    assert finding.title == "Upstairs: AC may not be working"


def test_cooling_that_cools_is_fine(conn):
    calling(conn, "cooling", 50, before=26.0, after=25.2)
    assert keys(conn) == []


def test_duplicate_status_events_are_one_run():
    t = [ago(m) for m in (30, 29, 28, 20, 19, 10)]
    statuses = list(zip(t, ["cooling", "idle", "idle", "idle", "cooling", "cooling"], strict=True))
    assert health.runs(statuses) == [
        health.Run("cooling", t[0], t[1]),
        health.Run("idle", t[1], t[4]),
        health.Run("cooling", t[4], None),
    ]


@pytest.mark.parametrize(("short_runs", "expected"), [(2, []), (3, ["short_cycling:up"])])
def test_short_cycling(conn, short_runs, expected):
    every_15_min(conn, 120, ambient=18.0)
    for n in range(short_runs):
        start = 100 - n * 30
        event(conn, ago(start), hvac="heating")
        event(conn, ago(start - 3), hvac="idle", temp=18.3)
    event(conn, ago(5), temp=18.0)
    assert keys(conn) == expected


def test_long_runs_are_not_short_cycling(conn):
    every_15_min(conn, 120, ambient=18.0)
    for start in (110, 70, 30):
        event(conn, ago(start), hvac="heating")
        event(conn, ago(start - 15), hvac="idle", temp=19.0)
    assert keys(conn) == []


def test_below_the_floor_is_urgent(conn):
    every_15_min(conn, 30, ambient=12.5, name="Living Room", device="down")
    (finding,) = health.check(conn, NOW, events_enabled=False)
    assert finding.key == "below_floor:down" and finding.priority == "urgent"
    assert finding.title == "Living Room is too cold"
    assert "54.5°F, below the 55.0°F safety floor" in finding.message


def test_above_the_ceiling(conn):
    every_15_min(conn, 30, ambient=31.0)
    assert keys(conn) == ["above_ceiling:up"]


@pytest.mark.parametrize(("offline_for", "expected"), [(45, []), (75, ["offline:up"])])
def test_offline(conn, offline_for, expected):
    every_15_min(conn, 180, until=offline_for + 15)
    every_15_min(conn, offline_for, online=False)
    assert keys(conn) == expected


def test_back_online_clears(conn):
    every_15_min(conn, 180, online=False)
    snap(conn, NOW, online=True)
    assert keys(conn) == []


def test_stopped_snapshots_alert_once_not_per_device(conn):
    every_15_min(conn, 300, ambient=18.0, hvac="heating")  # would look like a fault...
    conn.execute("DELETE FROM thermostat_snapshot WHERE taken_at > ?", (iso(ago(120)),))
    (finding,) = health.check(conn, NOW, events_enabled=False)  # ...but the data is stale
    assert finding.key == "stale:thermostats" and "for 2 h" in finding.message


def test_stopped_weather(conn):
    every_15_min(conn, 30)
    conn.execute(
        "INSERT INTO weather_observation (observed_at, fetched_at) VALUES (?, ?)",
        (iso(ago(180)), iso(ago(178))),
    )
    assert keys(conn) == ["stale:weather"]


def test_stopped_events(conn):
    every_15_min(conn, 13 * 60)
    event(conn, ago(13 * 60), temp=20.0)
    assert keys(conn, events_enabled=False) == []
    (finding,) = health.check(conn, NOW, events_enabled=True)
    assert finding.key == "stale:events" and "for 13 h" in finding.message


def test_no_events_ever(conn):
    every_15_min(conn, 13 * 60)
    (finding,) = health.check(conn, NOW, events_enabled=True)
    assert "No thermostat events ever" in finding.message


def test_events_are_not_expected_on_day_one(conn):
    every_15_min(conn, 60)
    assert keys(conn, events_enabled=True) == []


@pytest.mark.parametrize(
    ("floor", "ceiling", "expected"),
    [
        (None, None, (12.78, 30.0)),  # 55°F and 86°F
        ("60F", "80 °F", (15.56, 26.67)),
        ("10c", "28.5C", (10.0, 28.5)),
    ],
)
def test_limits_from_settings(env_file, floor, ceiling, expected):
    lines = (f"ALERT_FLOOR={floor}\n" if floor else "") + (
        f"ALERT_CEILING={ceiling}\n" if ceiling else ""
    )
    env_file.write_text(env_file.read_text() + lines)
    limits = health.Limits.from_settings()
    assert (round(limits.floor_c, 2), round(limits.ceiling_c, 2)) == expected


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ("ALERT_FLOOR=55\n", "ALERT_FLOOR must be a temperature with its unit"),
        ("ALERT_CEILING=hot\n", "ALERT_CEILING must be"),
        ("ALERT_FLOOR=90F\n", "ALERT_FLOOR must be below ALERT_CEILING"),
    ],
)
def test_limits_errors(env_file, lines, message):
    env_file.write_text(env_file.read_text() + lines)
    with pytest.raises(ConfigError, match=message):
        health.Limits.from_settings()


def test_a_higher_floor_alerts_sooner(conn):
    every_15_min(conn, 30, ambient=14.0)  # 57.2°F: above the default floor
    assert keys(conn) == []
    limits = health.Limits(health.celsius("60F", "ALERT_FLOOR"), 30.0)
    (finding,) = health.check(conn, NOW, False, limits)
    assert finding.key == "below_floor:up" and "below the 60.0°F safety floor" in finding.message
