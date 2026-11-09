from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from test_health import NOW, ago, event, iso, snap

from climatectl import health, store


@pytest.fixture
def conn(tmp_path):
    # Not the test machine's timezone: local days must come from utc_offset, not SQLite.
    return store.connect(tmp_path / "climatectl.db", ZoneInfo("America/New_York"))


def evening_with_cooling(conn):
    """An evening upstairs: two cooling runs and a burst of OFF repeats."""
    snap(conn, ago(60), ambient=20.8)
    snap(conn, ago(45), ambient=20.8)
    event(conn, ago(42), hvac="cooling")
    event(conn, ago(35), hvac="idle")
    event(conn, ago(34), hvac="idle", temp=20.7)
    event(conn, ago(31), hvac="cooling")
    snap(conn, ago(30), ambient=20.7, hvac="cooling")
    snap(conn, ago(15), ambient=20.4, hvac="cooling")
    event(conn, ago(1), hvac="idle", temp=20.2)
    snap(conn, NOW, ambient=20.2)


def test_hvac_run_matches_the_health_checks(conn):
    evening_with_cooling(conn)
    rows = conn.execute(
        "SELECT name, status, start_at, end_at FROM hvac_run ORDER BY start_at"
    ).fetchall()
    (device,) = health._devices(conn, ago(120))
    expected = [
        ("Upstairs", r.status, iso(r.start), r.end and iso(r.end))
        for r in health.runs(device.statuses)
    ]
    assert rows == expected
    assert [r[1] for r in rows] == ["idle", "cooling", "idle", "cooling", "idle"]


def test_hvac_cycle_has_temperatures_and_rate(conn):
    evening_with_cooling(conn)
    conn.execute(
        "INSERT INTO weather_observation (observed_at, fetched_at, temp_c) VALUES (?, ?, 16.0)",
        (iso(ago(45)), iso(ago(45))),
    )
    cycles = conn.execute(
        "SELECT status, round(minutes), start_temp_c, end_temp_c, outdoor_c,"
        " round(rate_c_per_h, 2) FROM hvac_cycle ORDER BY start_at"
    ).fetchall()
    assert cycles == [
        ("cooling", 7.0, 20.8, 20.8, 16.0, 0.0),
        ("cooling", 30.0, 20.7, 20.2, 16.0, -1.0),
    ]


def test_indoor_reading_merges_snapshots_and_events(conn):
    snap(conn, ago(30), ambient=18.0)
    event(conn, ago(20), temp=18.4)
    event(conn, ago(10), hvac="heating")  # no temperature: not a reading
    snap(conn, NOW, ambient=18.9)
    assert conn.execute("SELECT at, temp_c FROM indoor_reading ORDER BY at").fetchall() == [
        (iso(ago(30)), 18.0),
        (iso(ago(20)), 18.4),
        (iso(NOW), 18.9),
    ]


def test_offline_snapshots_are_not_readings(conn):
    # Saturday upstairs: offline for hours, every snapshot repeating the last reading.
    snap(conn, ago(60), ambient=19.8)
    snap(conn, ago(45), ambient=19.8, online=False)
    snap(conn, ago(30), ambient=19.8, online=False)
    snap(conn, NOW, ambient=20.6)
    conn.execute("UPDATE thermostat_snapshot SET humidity_pct = 53.0")
    assert conn.execute("SELECT at FROM indoor_reading ORDER BY at").fetchall() == [
        (iso(ago(60)),),
        (iso(NOW),),
    ]
    assert conn.execute("SELECT at FROM dew_point ORDER BY at").fetchall() == [
        (iso(ago(60)),),
        (iso(NOW),),
    ]


def test_hvac_daily_uses_local_days(conn):
    # 00:30 and 03:30 UTC on Oct 30 are still Oct 29 in New York (UTC-4).
    snap(conn, ago(11.5 * 60 + 5))
    for start, minutes in (("2026-10-30T00:30:00Z", 20), ("2026-10-30T03:30:00Z", 10)):
        event(conn, store.parse_utc(start), hvac="heating")
        event(conn, store.parse_utc(start) + timedelta(minutes=minutes), hvac="idle")
    event(conn, ago(60), hvac="cooling")
    event(conn, ago(15), hvac="idle")
    assert conn.execute(
        "SELECT day, name, round(heating_min, 1), round(cooling_min, 1) FROM hvac_daily"
        " ORDER BY day"
    ).fetchall() == [
        ("2026-10-29T04:00:00Z", "Upstairs", 30.0, 0.0),
        ("2026-10-30T04:00:00Z", "Upstairs", 0.0, 45.0),
    ]


def test_weather_daily_degree_days(conn):
    for at, temp_c in (("2026-10-30T12:00:00Z", 10.0), ("2026-10-30T18:00:00Z", 20.0)):
        conn.execute(
            "INSERT INTO weather_observation (observed_at, fetched_at, temp_c) VALUES (?, ?, ?)",
            (at, at, temp_c),
        )
    row = conn.execute(
        "SELECT day, avg_c, min_c, max_c, round(hdd_f, 1), cdd_f FROM weather_daily"
    ).fetchone()
    assert row == ("2026-10-30T04:00:00Z", 15.0, 10.0, 20.0, 6.0, 0)


def test_dew_point_indoors_and_outdoors(conn):
    # A mild evening: upstairs 69.6°F at 54%, outside 63.1°F at 59%.
    snap(conn, NOW, ambient=20.89)
    conn.execute("UPDATE thermostat_snapshot SET humidity_pct = 54.0")
    conn.execute(
        "INSERT INTO weather_observation (observed_at, fetched_at, temp_c, humidity_pct)"
        " VALUES (?, ?, 17.3, 59.0)",
        (iso(NOW), iso(NOW)),
    )
    rows = conn.execute(
        "SELECT level, round(dew_point_c * 9 / 5 + 32, 1) FROM dew_point ORDER BY level"
    ).fetchall()
    assert rows == [("Outdoor", 48.6), ("Upstairs", 52.2)]


def test_local_days_follow_daylight_saving():
    days = dict(store.local_days(ZoneInfo("America/New_York")))
    assert days["2026-03-08T05:00:00Z"] == "2026-03-08"  # EST midnight; EDT from 2 AM
    assert days["2026-03-09T04:00:00Z"] == "2026-03-09"
    assert days["2026-11-01T04:00:00Z"] == "2026-11-01"  # EDT midnight; EST from 2 AM
    assert days["2026-11-02T05:00:00Z"] == "2026-11-02"
    assert len(days) == 7305  # 2020 through 2039


def test_local_days_across_the_dst_change(conn):
    # Clocks go back at 2 AM on Nov 1: one run on Oct 31, two on Nov 1 either side of the
    # change (1:30 AM EDT and 1:30 AM EST). Both Nov 1 runs are one day.
    snap(conn, store.parse_utc("2026-11-01T03:30:00Z"))
    for at in ("2026-11-01T03:30:00Z", "2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"):
        event(conn, store.parse_utc(at), hvac="heating")
        event(conn, store.parse_utc(at) + timedelta(minutes=10), hvac="idle")
    assert conn.execute(
        "SELECT day, round(heating_min) FROM hvac_daily ORDER BY day"
    ).fetchall() == [("2026-10-31T04:00:00Z", 10.0), ("2026-11-01T04:00:00Z", 20.0)]


def test_setpoints_from_snapshots_and_events(conn):
    def settings(at, mode, eco):
        conn.execute(
            "INSERT INTO thermostat_snapshot (taken_at, device_id, name, mode, eco, heat_c,"
            " cool_c, eco_heat_c, eco_cool_c) VALUES (?, 'up', 'Upstairs', ?, ?, 17.8, 22.8,"
            " 10, 24.4)",
            (iso(at), mode, eco),
        )

    snap(conn, ago(60))  # no mode in this snapshot: no setpoints apply
    settings(ago(45), "heat_cool", False)
    settings(ago(30), "heat", True)
    conn.execute(
        "INSERT INTO thermostat_event (event_id, at, device_id, heat_c, cool_c)"
        " VALUES ('sp', ?, 'up', 18.0, 23.0)",
        (iso(ago(10)),),
    )
    event(conn, ago(5), temp=18.2)  # no setpoint in this event
    assert conn.execute("SELECT at, heat_c, cool_c FROM setpoint ORDER BY at").fetchall() == [
        (iso(ago(60)), None, None),
        (iso(ago(45)), 17.8, 22.8),
        (iso(ago(30)), 10.0, None),  # Eco, heat-only mode
        (iso(ago(10)), 18.0, 23.0),
    ]
