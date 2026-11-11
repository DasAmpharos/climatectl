"""Write three simulated days for two thermostats into a climatectl database, ending now, so
the dashboard has something to show before a real thermostat is connected (and CI can check
every panel's query). Run from a climatectl checkout:

    uv run python deploy/demo_data.py data/demo.db

then point Grafana at it (see "Trying the dashboard" in the README). Nothing here is real."""

from __future__ import annotations

import argparse
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from climatectl import store
from climatectl.thermostat import COOLING, HEAT_COOL, HEATING, IDLE, Thermostat, ThermostatEvent
from climatectl.weather import Weather

ROOMS = {  # device id: (name, solar gain in °C per hour at the afternoon peak)
    "demo-living": ("Living room", 0.4),
    "demo-bedroom": ("Bedroom", 1.6),
}
DAYS = 3
HEAT_DAY, HEAT_NIGHT, COOL = 20.5, 18.0, 23.5
HEAT_RATE, COOL_RATE, LOSS = 2.4, 2.0, 0.035  # °C per hour, and the share of the gap per hour
HYSTERESIS = 0.3


def iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def outdoor(at: datetime, zone: ZoneInfo) -> tuple[float, float, float]:
    """Temperature, humidity and sun for a mild autumn: coldest at 6, warmest at 15."""
    local = at.astimezone(zone)
    hour = local.hour + local.minute / 60
    temp = 11 + 8 * math.sin((hour - 9) / 24 * 2 * math.pi)
    sun = max(0.0, math.sin((hour - 7) / 12 * math.pi)) if 7 <= hour <= 19 else 0.0
    return temp, 75 - 2.2 * (temp - 11), sun


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path)
    parser.add_argument("--timezone", default="America/New_York")
    args = parser.parse_args()
    zone = ZoneInfo(args.timezone)
    rng = random.Random(7)
    conn = store.connect(args.path, zone)

    end = datetime.now(UTC).replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 15)
    at = end - timedelta(days=DAYS)
    state = {d: {"temp": 19.5, "hvac": IDLE, "reported": 19.5} for d in ROOMS}
    snapshots, events = [], []
    while at <= end:
        temp_out, humidity_out, sun = outdoor(at, zone)
        local_hour = at.astimezone(zone).hour
        heat = HEAT_DAY if 6 <= local_hour < 22 else HEAT_NIGHT
        if at.minute % 15 == 0:
            store.save_observation(
                conn,
                Weather(iso(at), round(temp_out, 1), round(humidity_out), 40.0,
                        round(800 * sun), 9.0, 0.0),
                iso(at),
            )  # fmt: skip
        for device, (name, gain) in ROOMS.items():
            s = state[device]
            drift = (temp_out - s["temp"]) * LOSS + gain * sun
            drift += HEAT_RATE if s["hvac"] == HEATING else 0
            drift -= COOL_RATE if s["hvac"] == COOLING else 0
            s["temp"] += drift / 60 + rng.gauss(0, 0.01)
            hvac = s["hvac"]
            if hvac == IDLE and s["temp"] < heat - HYSTERESIS:
                hvac = HEATING
            elif hvac == IDLE and s["temp"] > COOL + HYSTERESIS:
                hvac = COOLING
            elif (hvac == HEATING and s["temp"] >= heat + HYSTERESIS) or (
                hvac == COOLING and s["temp"] <= COOL - HYSTERESIS
            ):
                hvac = IDLE
            changed = {}
            if hvac != s["hvac"]:
                changed["hvac"] = s["hvac"] = hvac
            if abs(round(s["temp"], 1) - s["reported"]) >= 0.1:
                changed["temp_c"] = s["reported"] = round(s["temp"], 1)
            if changed:
                events.append(ThermostatEvent(f"{device}-{iso(at)}", iso(at), device, **changed))
            if at.minute % 15 == 0:
                snapshots.append(
                    (iso(at), Thermostat(device, name, True, s["reported"], 45.0 + rng.random() * 4,
                                         HEAT_COOL, s["hvac"], False, heat, COOL, 15.5, 27.0))
                )  # fmt: skip
        at += timedelta(minutes=1)

    for taken_at, thermostat in snapshots:
        store.save_snapshot(conn, taken_at, [thermostat])
    store.save_events(conn, events)
    opened = end - timedelta(days=1, hours=3)
    conn.execute(
        "INSERT INTO alert (key, title, message, priority, opened_at, last_seen_at, notified_at,"
        " resolved_at) VALUES ('short_cycling:demo-bedroom', 'Bedroom: short cycling',"
        " '3 runs under 5 min in the last 2 h (demo).', 'high', ?, ?, ?, ?)",
        (iso(opened), iso(opened + timedelta(minutes=45)), iso(opened),
         iso(opened + timedelta(hours=1, minutes=15))),
    )  # fmt: skip
    conn.commit()
    runs = conn.execute("SELECT status, count(*) FROM hvac_cycle GROUP BY 1").fetchall()
    print(f"{args.path}: {len(snapshots)} snapshots, {len(events)} events, runs {dict(runs)}")


if __name__ == "__main__":
    main()
