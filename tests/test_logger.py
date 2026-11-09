import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import requests
from conftest import (
    HEALTHCHECK_URL,
    HOME_LATITUDE,
    HOME_LONGITUDE,
    PUSHOVER_APP_TOKEN,
    PUSHOVER_USER_KEY,
)
from fake_pubsub import SUBSCRIPTION, FakePubSub, hvac

from climatectl import logger_cli, providers, store, weather
from climatectl.providers.nest import NestEvents, pubsub

NOW = datetime(2026, 10, 24, 0, 47, 3, tzinfo=UTC)
HOME = weather.Location(51.50, -0.12)
NEST = providers.load("nest")


@pytest.fixture
def conn(tmp_path):
    conn = store.connect(tmp_path / "data" / "climatectl.db")
    NEST.prepare(conn)
    return conn


def rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()


def test_first_cycle_logs_everything(fakes, authorized, conn):
    summary = logger_cli.run_cycle(conn, HOME, NOW, NEST)
    assert summary == [
        "weather 15.5°C at 2026-10-24T00:45:00Z",
        "2 thermostat(s)",
        "48h forecast",
    ]
    assert rows(conn, "weather_observation") == [
        ("2026-10-24T00:45:00Z", "2026-10-24T00:47:03Z", 15.5, 49.0, 0.0, 0.0, 7.8, 0.0)
    ]
    snapshot = rows(conn, "thermostat_snapshot")
    assert [(r[0], r[1], r[2], r[3], r[4], r[6]) for r in snapshot] == [
        ("2026-10-24T00:47:03Z", "down", "Downstairs", 1, 21.3, "heat"),
        ("2026-10-24T00:47:03Z", "up", "Upstairs", 1, 22.0, "heat_cool"),
    ]
    forecast = rows(conn, "weather_forecast")
    assert len(forecast) == 48
    assert forecast[0][:3] == ("2026-10-24T00:47:03Z", "2026-10-24T00:00:00Z", 15.5)


def test_forecast_only_every_six_hours(fakes, authorized, conn):
    logger_cli.run_cycle(conn, HOME, NOW, NEST)
    assert "48h forecast" not in logger_cli.run_cycle(conn, HOME, NOW + timedelta(minutes=15), NEST)
    assert "48h forecast" in logger_cli.run_cycle(conn, HOME, NOW + timedelta(hours=6), NEST)
    issued = {r[0] for r in rows(conn, "weather_forecast")}
    assert len(issued) == 2


def test_unchanged_weather_is_not_duplicated(fakes, authorized, conn):
    logger_cli.run_cycle(conn, HOME, NOW, NEST)
    summary = logger_cli.run_cycle(conn, HOME, NOW + timedelta(minutes=5), NEST)
    assert summary[0] == "weather 15.5°C at 2026-10-24T00:45:00Z (unchanged)"
    assert len(rows(conn, "weather_observation")) == 1


def test_nest_failure_does_not_stop_weather(fakes, authorized, conn):
    google, _ = fakes
    google.refresh_revoked = True
    summary = logger_cli.run_cycle(conn, HOME, NOW, NEST)
    assert summary == [
        "weather 15.5°C at 2026-10-24T00:45:00Z",
        "thermostats FAILED",
        "48h forecast",
    ]
    assert len(rows(conn, "weather_observation")) == 1
    assert rows(conn, "thermostat_snapshot") == []


def test_weather_failure_does_not_stop_thermostats(fakes, authorized, conn):
    _, meteo = fakes
    meteo.fail = True
    summary = logger_cli.run_cycle(conn, HOME, NOW, NEST)
    assert summary == ["weather FAILED", "2 thermostat(s)", "forecast FAILED"]
    assert len(rows(conn, "thermostat_snapshot")) == 2


@pytest.mark.parametrize(
    ("now", "wake"),
    [
        ("00:47:03", "01:02:00"),
        ("01:01:59", "01:02:00"),
        ("01:02:00", "01:17:00"),
        ("23:58:00", "00:02:00"),
    ],
)
def test_next_wake(now, wake):
    day = datetime(2026, 10, 24, tzinfo=UTC)
    current = datetime.combine(day.date(), datetime.strptime(now, "%H:%M:%S").time(), UTC)
    got = logger_cli.next_wake(current)
    assert got.strftime("%H:%M:%S") == wake
    assert timedelta(0) < got - current <= timedelta(minutes=15)


def test_main_once(fakes, authorized, tmp_path, monkeypatch, caplog):
    db = tmp_path / "custom.db"
    monkeypatch.setenv("CLIMATECTL_DB", str(db))
    with caplog.at_level("INFO"):
        logger_cli.main(["--once"])
    assert "cycle: weather 15.5°C" in caplog.text
    assert db.exists()
    for text in (HOME_LATITUDE, HOME_LONGITUDE, "51.50", "-0.12"):
        assert text not in caplog.text


def test_main_needs_location(fakes, authorized):
    authorized.write_text(authorized.read_text().replace("HOME_LATITUDE", "#HOME_LATITUDE"))
    with pytest.raises(SystemExit, match="HOME_LATITUDE"):
        logger_cli.main(["--once"])


@pytest.fixture
def events():
    fake = FakePubSub()
    return fake, NestEvents(pubsub.Subscriber(SUBSCRIPTION, session=fake))


def test_events_are_stored_then_acknowledged(fakes, authorized, conn, events):
    fake, events = events
    fake.publish(hvac("evt-1", "2026-10-24T00:31:10.5Z", "down", "HEATING"))
    fake.publish(hvac("evt-2", "2026-10-24T00:44:59.9Z", "down", "OFF"))
    summary = logger_cli.run_cycle(conn, HOME, NOW, NEST, events)
    assert "2 new event(s)" in summary
    assert fake.waiting == {} and fake.acked == ["ack-1", "ack-2"]
    stored = conn.execute(
        "SELECT event_id, event_at, received_at, device_id, kind, traits_json FROM sdm_event"
        " ORDER BY event_at"
    ).fetchall()
    assert stored == [
        ("evt-1", "2026-10-24T00:31:10Z", "2026-10-24T00:47:03Z", "down", "resourceUpdate",
         json.dumps({"sdm.devices.traits.ThermostatHvac": {"status": "HEATING"}})),
        ("evt-2", "2026-10-24T00:44:59Z", "2026-10-24T00:47:03Z", "down", "resourceUpdate",
         json.dumps({"sdm.devices.traits.ThermostatHvac": {"status": "OFF"}})),
    ]  # fmt: skip
    assert conn.execute(
        "SELECT event_id, at, device_id, hvac FROM thermostat_event"
    ).fetchall() == [
        ("evt-1", "2026-10-24T00:31:10Z", "down", "heating"),
        ("evt-2", "2026-10-24T00:44:59Z", "down", "idle"),
    ]
    assert "0 new event(s)" in logger_cli.run_cycle(conn, HOME, NOW, NEST, events)


def test_redelivered_event_is_stored_once(fakes, authorized, conn, events):
    fake, events = events
    fake.publish(hvac("evt-1", "2026-10-24T00:31:10Z", "down", "HEATING"))
    fake.publish(hvac("evt-1", "2026-10-24T00:31:10Z", "down", "HEATING"))  # Pub/Sub retry
    assert "1 new event(s)" in logger_cli.run_cycle(conn, HOME, NOW, NEST, events)
    assert fake.waiting == {}
    assert conn.execute("SELECT count(*) FROM sdm_event").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM thermostat_event").fetchone() == (1,)


def test_events_drain_in_batches(fakes, authorized, conn, events):
    fake, events = events
    for n in range(250):
        fake.publish(hvac(f"evt-{n}", "2026-10-24T00:00:00Z", "down", "OFF"))
    assert "250 new event(s)" in logger_cli.run_cycle(conn, HOME, NOW, NEST, events)
    assert fake.calls.count("pull") == 4  # 100 + 100 + 50, then an empty pull


def test_pubsub_failure_does_not_stop_the_rest(fakes, authorized, conn, events):
    fake, events = events
    fake.publish(hvac("evt-1", "2026-10-24T00:31:10Z", "down", "HEATING"))
    fake.fail = True
    summary = logger_cli.run_cycle(conn, HOME, NOW, NEST, events)
    assert summary == [
        "weather 15.5°C at 2026-10-24T00:45:00Z",
        "2 thermostat(s)",
        "events FAILED",
        "48h forecast",
    ]
    assert list(fake.waiting) == ["ack-1"]  # still waiting for the next cycle


def test_main_without_a_project_disables_events(fakes, authorized, caplog):
    with caplog.at_level("INFO"):
        logger_cli.main(["--once"])
    assert "events disabled: Missing in" in caplog.text
    assert "new event(s)" not in caplog.text


def test_main_with_a_subscription(fakes, authorized, monkeypatch, caplog):
    fake = FakePubSub()
    fake.publish(hvac("evt-1", "2026-10-24T00:31:10Z", "down", "HEATING"))
    monkeypatch.setattr(pubsub, "default_session", lambda: fake)
    authorized.write_text(authorized.read_text() + "GCP_PROJECT_ID=climatectl-123\n")
    with caplog.at_level("INFO"):
        logger_cli.main(["--once"])
    assert "1 new event(s)" in caplog.text


def test_heartbeat_after_each_cycle(fakes, authorized, pings, caplog):
    authorized.write_text(authorized.read_text() + f"HEALTHCHECK_URL={HEALTHCHECK_URL}\n")
    with caplog.at_level("INFO"):
        logger_cli.main(["--once"])
    assert pings == [HEALTHCHECK_URL]
    assert HEALTHCHECK_URL not in caplog.text  # the URL is a credential


def test_no_heartbeat_when_unset(fakes, authorized, pings):
    logger_cli.main(["--once"])
    assert pings == []


def test_failed_heartbeat_is_only_logged(monkeypatch, caplog):
    def unreachable(url, **_):
        raise requests.ConnectionError("hc-ping.com unreachable")

    monkeypatch.setattr(requests, "get", unreachable)
    with caplog.at_level("WARNING"):
        logger_cli.heartbeat(HEALTHCHECK_URL)
    assert "heartbeat failed: ConnectionError" in caplog.text
    assert HEALTHCHECK_URL not in caplog.text


def test_furnace_fault_alerts_through_pushover(fakes, authorized, conn, pushover_posts):
    # The fake downstairs thermostat calls for heat at a constant 21.3°C.
    send = logger_cli.alerts.pushover(PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY)
    for minutes in range(0, 46, 15):
        summary = logger_cli.run_cycle(
            conn, HOME, NOW + timedelta(minutes=minutes), NEST, None, send
        )
    assert summary[-1] == "1 alert(s) open"
    (post,) = pushover_posts
    assert post["data"]["title"] == "Downstairs: furnace may not be working"


def test_main_sends_alerts_when_configured(fakes, authorized, pushover_posts, caplog):
    authorized.write_text(
        authorized.read_text()
        + f"PUSHOVER_APP_TOKEN={PUSHOVER_APP_TOKEN}\nPUSHOVER_USER_KEY={PUSHOVER_USER_KEY}\n"
    )
    with caplog.at_level("INFO"):
        logger_cli.main(["--once"])
    assert "not set: alerts are logged" not in caplog.text
    assert pushover_posts == []  # nothing wrong after one cycle
    assert PUSHOVER_APP_TOKEN not in caplog.text and PUSHOVER_USER_KEY not in caplog.text


def test_main_warns_when_alerts_have_nowhere_to_go(fakes, authorized, caplog):
    with caplog.at_level("INFO"):
        logger_cli.main(["--once"])
    assert "PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY not set" in caplog.text


def test_main_rejects_a_bad_alert_floor(fakes, authorized):
    authorized.write_text(authorized.read_text() + "ALERT_FLOOR=cold\n")
    with pytest.raises(SystemExit, match="ALERT_FLOOR must be a temperature"):
        logger_cli.main(["--once"])
