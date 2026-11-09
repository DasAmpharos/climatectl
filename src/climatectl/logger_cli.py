"""`climatectl logger`: the observer. Only writes to SQLite; never changes a thermostat.

Every 15 minutes it logs current weather, a snapshot of each thermostat and every event the
thermostat provider (THERMOSTAT_PROVIDER, default nest) has waiting, e.g. Nest's Pub/Sub
subscription. Events carry their own timestamps, so draining them every 15 minutes still
records exactly when the furnace or AC started and stopped. Every 6 hours it also logs a
48-hour hourly forecast. Each job runs on its own, so a thermostat outage doesn't stop weather
logging and vice versa. After the jobs, the health checks look for HVAC faults and
stopped data, and alert through Pushover if PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY are set.
If HEALTHCHECK_URL is set, every cycle ends with a ping to it."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta

import requests

from . import alerts, envfile, health, providers, store, weather
from .envfile import ConfigError
from .providers import EventSource, Provider, ProviderError

log = logging.getLogger("climatectl")

INTERVAL = timedelta(minutes=15)
# Open-Meteo refreshes "current" on the quarter hour; wake a little after so it's ready.
OFFSET = timedelta(minutes=2)
FORECAST_EVERY = timedelta(hours=6)
FORECAST_HOURS = 48
PUSHOVER_SETTINGS = ("PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY")
EXPECTED_ERRORS = (ConfigError, ProviderError, weather.WeatherError)


def run_cycle(
    conn: sqlite3.Connection,
    location: weather.Location,
    now: datetime,
    provider: Provider,
    events: EventSource | None = None,
    send: alerts.Sender | None = None,
    limits: health.Limits = health.DEFAULT_LIMITS,
) -> list[str]:
    """One pass of every job that is due. Returns a short summary per job."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = []

    try:
        obs = weather.current(location)
        new = store.save_observation(conn, obs, stamp)
        summary.append(f"weather {obs.temp_c}°C at {obs.at}{'' if new else ' (unchanged)'}")
    except EXPECTED_ERRORS as e:
        log.warning("weather: %s", e)
        summary.append("weather FAILED")

    try:
        found = provider.thermostats()
        store.save_snapshot(conn, stamp, found)
        summary.append(f"{len(found)} thermostat(s)")
    except EXPECTED_ERRORS as e:
        log.warning("thermostats: %s", e)
        summary.append("thermostats FAILED")

    if events is not None:
        try:
            summary.append(f"{events.drain(conn, stamp)} new event(s)")
        except EXPECTED_ERRORS as e:
            log.warning("events: %s", e)
            summary.append("events FAILED")

    last = store.last_forecast_at(conn)
    if last is None or now - last >= FORECAST_EVERY:
        try:
            store.save_forecast(conn, stamp, weather.forecast(location, FORECAST_HOURS))
            summary.append(f"{FORECAST_HOURS}h forecast")
        except EXPECTED_ERRORS as e:
            log.warning("forecast: %s", e)
            summary.append("forecast FAILED")

    findings = health.check(conn, now, events is not None, limits, provider.name, provider.hints)
    if open_alerts := alerts.update(conn, findings, now, send):
        summary.append(f"{open_alerts} alert(s) open")
    return summary


def next_wake(now: datetime) -> datetime:
    """The next quarter hour plus OFFSET, strictly after `now`."""
    base = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    wake = base + OFFSET
    while wake <= now:
        wake += INTERVAL
    return wake


def heartbeat(url: str) -> None:
    """Tell the dead-man's switch (e.g. healthchecks.io) that a cycle ran. Only the pings
    stopping raises an alert, so one failed ping is just logged. Never log the URL: anyone
    holding it could send pings on our behalf."""
    try:
        requests.get(url, timeout=10)
    except requests.RequestException as e:
        log.warning("heartbeat failed: %s", type(e).__name__)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="climatectl logger", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        location = weather.Location.from_settings()
        limits = health.Limits.from_settings()
        provider = providers.from_settings()
    except ConfigError as e:
        sys.exit(str(e))
    try:
        events = provider.events()
    except ConfigError as e:
        log.warning("events disabled: %s", e)
        events = None
    settings = envfile.read_settings(("HEALTHCHECK_URL", *PUSHOVER_SETTINGS))
    heartbeat_url = settings.get("HEALTHCHECK_URL")
    if all(k in settings for k in PUSHOVER_SETTINGS):
        send = alerts.pushover(*(settings[k] for k in PUSHOVER_SETTINGS))
    else:
        send = None
        log.warning(
            "%s not set: alerts are logged and stored, not sent", " and ".join(PUSHOVER_SETTINGS)
        )
    path = store.db_path()
    conn = store.connect(path)
    provider.prepare(conn)
    log.info("logging %s thermostats to %s", provider.name, path)

    while True:
        now = datetime.now(UTC)
        summary = run_cycle(conn, location, now, provider, events, send, limits)
        log.info("cycle: %s", ", ".join(summary))
        if heartbeat_url:
            heartbeat(heartbeat_url)
        if args.once:
            return
        wake = next_wake(datetime.now(UTC))
        time.sleep((wake - datetime.now(UTC)).total_seconds())


if __name__ == "__main__":
    main()
