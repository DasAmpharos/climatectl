"""SQLite log of everything the service observes. Every timestamp is UTC ISO 8601 ending in Z.

Re-running a cycle is harmless: rows are keyed on what they describe (the observation time,
the forecast issue/valid time, the snapshot time and device, the event id), so duplicates
are ignored."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import envfile
from .envfile import ConfigError
from .thermostat import Thermostat, ThermostatEvent
from .weather import Weather

SNAPSHOT_COLUMNS = """
    taken_at     TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    name TEXT, online INTEGER, ambient_c REAL, humidity_pct REAL, mode TEXT, hvac TEXT,
    eco INTEGER, heat_c REAL, cool_c REAL, eco_heat_c REAL, eco_cool_c REAL,
    PRIMARY KEY (taken_at, device_id)
"""
SNAPSHOT_INDEX = """CREATE INDEX IF NOT EXISTS thermostat_snapshot_device_time
    ON thermostat_snapshot (device_id, taken_at);"""
# Values use the neutral vocabulary in thermostat.py; eco is 0 or 1.
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS weather_observation (
    observed_at  TEXT PRIMARY KEY,
    fetched_at   TEXT NOT NULL,
    temp_c REAL, humidity_pct REAL, cloud_pct REAL, solar_wm2 REAL, wind_kmh REAL, precip_mm REAL
);
CREATE TABLE IF NOT EXISTS weather_forecast (
    issued_at    TEXT NOT NULL,
    valid_at     TEXT NOT NULL,
    temp_c REAL, humidity_pct REAL, cloud_pct REAL, solar_wm2 REAL, wind_kmh REAL, precip_mm REAL,
    PRIMARY KEY (issued_at, valid_at)
);
CREATE TABLE IF NOT EXISTS thermostat_snapshot ({SNAPSHOT_COLUMNS});
-- Changes pushed between snapshots (see providers.EventSource), one row per event with only
-- the fields it changed. Providers may keep their raw events in tables of their own.
CREATE TABLE IF NOT EXISTS thermostat_event (
    event_id     TEXT PRIMARY KEY,  -- the provider's id for the event
    at           TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    temp_c REAL, humidity_pct REAL, hvac TEXT, heat_c REAL, cool_c REAL, mode TEXT, eco INTEGER
);
CREATE INDEX IF NOT EXISTS thermostat_event_device_time ON thermostat_event (device_id, at);
CREATE INDEX IF NOT EXISTS thermostat_event_time ON thermostat_event (at);
CREATE TABLE IF NOT EXISTS alert (
    id           INTEGER PRIMARY KEY,
    key          TEXT NOT NULL,  -- check and device, e.g. heat_no_rise:<device id>
    title TEXT NOT NULL, message TEXT NOT NULL, priority TEXT NOT NULL,
    opened_at    TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notified_at  TEXT,           -- last notification sent; NULL until one goes out
    resolved_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS alert_open ON alert (key) WHERE resolved_at IS NULL;
{SNAPSHOT_INDEX}
-- Local midnight of every day in LOCAL_DAY_YEARS in the home timezone, as UTC; rebuilt on every
-- connect. The views find a time's local day here. SQLite's 'localtime' would depend on the
-- reader (Grafana's SQLite plugin ignores TZ and treats local time as UTC).
DROP TABLE IF EXISTS utc_offset;
CREATE TABLE IF NOT EXISTS local_day (
    start_at     TEXT PRIMARY KEY,  -- local midnight, UTC
    date         TEXT NOT NULL      -- the local date, e.g. 2026-10-30
);
"""
DEFAULT_TIMEZONE = "UTC"
LOCAL_DAY_YEARS = (2020, 2040)

# Read-only views for reports and the Grafana dashboards. Recreated on every connect, so a new
# release can change them. Temperatures stay in Celsius; dashboards convert. Days are local
# days in the home timezone (see local_day).


def _day_of(column: str) -> str:
    """SQL for the local day `column` (qualify it with its table) falls in, as its local
    midnight in UTC."""
    return (
        f"(SELECT d.start_at FROM local_day d WHERE d.start_at <= {column}"
        " ORDER BY d.start_at DESC LIMIT 1)"
    )


DROP_VIEWS = """
DROP VIEW IF EXISTS thermostat;
DROP VIEW IF EXISTS indoor_reading;
DROP VIEW IF EXISTS setpoint;
DROP VIEW IF EXISTS hvac_status;
DROP VIEW IF EXISTS hvac_run;
DROP VIEW IF EXISTS hvac_cycle;
DROP VIEW IF EXISTS hvac_daily;
DROP VIEW IF EXISTS weather_daily;
DROP VIEW IF EXISTS dew_point;
"""
VIEWS = f"""
-- Each thermostat's latest name.
CREATE VIEW thermostat AS
SELECT device_id, name, max(taken_at) AS last_seen FROM thermostat_snapshot GROUP BY device_id;

-- Indoor temperature from snapshots and from events, in one series per device. Snapshots
-- taken while a thermostat is offline repeat its last reading, so they are left out.
CREATE VIEW indoor_reading AS
SELECT taken_at AS at, device_id, ambient_c AS temp_c FROM thermostat_snapshot
WHERE ambient_c IS NOT NULL AND online IS NOT 0
UNION ALL
SELECT at, device_id, temp_c FROM thermostat_event WHERE temp_c IS NOT NULL;

-- The setpoints in effect, from snapshots (the eco setpoints while in Eco; only those the
-- mode uses) and from setpoint-change events.
CREATE VIEW setpoint AS
SELECT taken_at AS at, device_id,
       CASE WHEN mode IN ('heat', 'heat_cool') THEN
           CASE WHEN eco THEN eco_heat_c ELSE heat_c END
       END AS heat_c,
       CASE WHEN mode IN ('cool', 'heat_cool') THEN
           CASE WHEN eco THEN eco_cool_c ELSE cool_c END
       END AS cool_c
FROM thermostat_snapshot
UNION ALL
SELECT at, device_id, heat_c, cool_c FROM thermostat_event
WHERE heat_c IS NOT NULL OR cool_c IS NOT NULL;

-- HVAC status (heating, cooling, idle) from snapshots and events: exact times from events,
-- snapshots fill in any that are missing. At the same second, the event (source 1) counts
-- as the later one, as in health.py.
CREATE VIEW hvac_status AS
SELECT taken_at AS at, device_id, hvac AS status, 0 AS source FROM thermostat_snapshot
WHERE hvac IS NOT NULL
UNION ALL
SELECT at, device_id, hvac, 1 FROM thermostat_event WHERE hvac IS NOT NULL;

-- One row per run of the same status (duplicates collapsed). end_at is NULL while it lasts;
-- minutes then counts up to now. Same rule as health.runs().
CREATE VIEW hvac_run AS
WITH ordered AS (
    SELECT at, source, device_id, status,
           lag(status) OVER (PARTITION BY device_id ORDER BY at, source) AS previous
    FROM hvac_status
), starts AS (
    SELECT at, source, device_id, status FROM ordered
    WHERE previous IS NULL OR previous <> status
), runs AS (
    SELECT device_id, status, at AS start_at,
           lead(at) OVER (PARTITION BY device_id ORDER BY at, source) AS end_at
    FROM starts
)
SELECT runs.device_id, thermostat.name, status, start_at, end_at,
       (julianday(coalesce(end_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))
        - julianday(start_at)) * 1440 AS minutes
FROM runs JOIN thermostat USING (device_id);

-- Finished heating and cooling runs with the indoor temperature at start and end, the outdoor
-- temperature at the start, and the rate (°C per hour; negative while cooling).
CREATE VIEW hvac_cycle AS
SELECT *, (end_temp_c - start_temp_c) / minutes * 60 AS rate_c_per_h
FROM (
    SELECT run.*,
        (SELECT temp_c FROM indoor_reading i WHERE i.device_id = run.device_id
         AND i.at <= run.start_at ORDER BY i.at DESC LIMIT 1) AS start_temp_c,
        (SELECT temp_c FROM indoor_reading i WHERE i.device_id = run.device_id
         AND i.at <= run.end_at ORDER BY i.at DESC LIMIT 1) AS end_temp_c,
        (SELECT temp_c FROM weather_observation w WHERE w.observed_at <= run.start_at
         ORDER BY w.observed_at DESC LIMIT 1) AS outdoor_c
    FROM hvac_run run
    WHERE status IN ('heating', 'cooling') AND end_at IS NOT NULL
);

-- Heating and cooling minutes per local day and device, by the day each run started. `day`
-- is local midnight as a UTC timestamp, so charts place it on the right date.
CREATE VIEW hvac_daily AS
SELECT {_day_of("run.start_at")} AS day, device_id, name,
       coalesce(sum(CASE WHEN status = 'heating' THEN minutes END), 0) AS heating_min,
       coalesce(sum(CASE WHEN status = 'cooling' THEN minutes END), 0) AS cooling_min
FROM hvac_run run
WHERE status IN ('heating', 'cooling')
GROUP BY 1, 2;

-- Outdoor temperature per local day, with heating and cooling degree-days (base 65°F).
CREATE VIEW weather_daily AS
SELECT {_day_of("w.observed_at")} AS day,
       avg(temp_c) AS avg_c, min(temp_c) AS min_c, max(temp_c) AS max_c,
       max(0, 65 - (avg(temp_c) * 9 / 5 + 32)) AS hdd_f,
       max(0, (avg(temp_c) * 9 / 5 + 32) - 65) AS cdd_f,
       count(*) AS observations
FROM weather_observation w
GROUP BY 1;

-- Dew point (Magnus formula) per level from snapshots, and outdoors as level 'Outdoor'. It
-- measures the water in the air, so unlike relative humidity it compares across temperatures:
-- open windows dry the house when the outdoor dew point is below the indoor one. Needs
-- SQLite's math functions (ln), which Python's SQLite and the Grafana plugin both have.
CREATE VIEW dew_point AS
SELECT at, level, 243.12 * g / (17.62 - g) AS dew_point_c
FROM (
    SELECT s.taken_at AS at, t.name AS level,
           ln(s.humidity_pct / 100.0) + 17.62 * s.ambient_c / (243.12 + s.ambient_c) AS g
    FROM thermostat_snapshot s JOIN thermostat t USING (device_id)
    WHERE s.ambient_c IS NOT NULL AND s.humidity_pct > 0 AND s.online IS NOT 0
    UNION ALL
    SELECT observed_at, 'Outdoor',
           ln(humidity_pct / 100.0) + 17.62 * temp_c / (243.12 + temp_c)
    FROM weather_observation
    WHERE temp_c IS NOT NULL AND humidity_pct > 0
);
"""
WEATHER_FIELDS = ("temp_c", "humidity_pct", "cloud_pct", "solar_wm2", "wind_kmh", "precip_mm")
SNAPSHOT_FIELDS = (
    "name", "online", "ambient_c", "humidity_pct", "mode", "hvac", "eco",
    "heat_c", "cool_c", "eco_heat_c", "eco_cool_c",
)  # fmt: skip
EVENT_FIELDS = ("temp_c", "humidity_pct", "hvac", "heat_c", "cool_c", "mode", "eco")


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> Path:
    """CLIMATECTL_DB if set, else data/climatectl.db next to the .env file."""
    explicit = envfile.read_settings(("CLIMATECTL_DB",)).get("CLIMATECTL_DB")
    return Path(explicit) if explicit else envfile.env_path().parent / "data" / "climatectl.db"


def home_timezone() -> ZoneInfo:
    """CLIMATECTL_TIMEZONE, an IANA name such as America/New_York, else UTC."""
    settings = envfile.read_settings(("CLIMATECTL_TIMEZONE",))
    name = settings.get("CLIMATECTL_TIMEZONE", DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        raise ConfigError(f"Not an IANA timezone: {name}") from None


def connect(path: Path, zone: ZoneInfo | None = None) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")  # readers (reports, Grafana) never block the logger
    conn.executescript(SCHEMA)
    conn.executescript(DROP_VIEWS)
    with conn:
        conn.execute("DELETE FROM local_day")
        conn.executemany("INSERT INTO local_day VALUES (?, ?)", local_days(zone or home_timezone()))
    conn.executescript(VIEWS)
    return conn


def local_days(zone: ZoneInfo) -> list[tuple[str, str]]:
    """(local midnight in UTC, local date) for every day in LOCAL_DAY_YEARS."""
    day = datetime(LOCAL_DAY_YEARS[0], 1, 1)
    days = []
    while day.year < LOCAL_DAY_YEARS[1]:
        midnight = day.replace(tzinfo=zone).astimezone(UTC)
        days.append((_iso(midnight), day.date().isoformat()))
        day += timedelta(days=1)
    return days


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def save_observation(conn: sqlite3.Connection, weather: Weather, fetched_at: str) -> bool:
    """True if this was a new observation (Open-Meteo only updates every 15 minutes)."""
    cur = conn.execute(
        f"INSERT OR IGNORE INTO weather_observation VALUES (?, ?, {_marks(WEATHER_FIELDS)})",
        (weather.at, fetched_at, *(getattr(weather, f) for f in WEATHER_FIELDS)),
    )
    conn.commit()
    return cur.rowcount == 1


def save_forecast(conn: sqlite3.Connection, issued_at: str, hours: Iterable[Weather]) -> None:
    conn.executemany(
        f"INSERT OR IGNORE INTO weather_forecast VALUES (?, ?, {_marks(WEATHER_FIELDS)})",
        [(issued_at, w.at, *(getattr(w, f) for f in WEATHER_FIELDS)) for w in hours],
    )
    conn.commit()


def last_forecast_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT max(issued_at) FROM weather_forecast").fetchone()
    return parse_utc(row[0]) if row[0] else None


def save_snapshot(
    conn: sqlite3.Connection, taken_at: str, thermostats: Iterable[Thermostat]
) -> None:
    conn.executemany(
        f"INSERT OR IGNORE INTO thermostat_snapshot VALUES (?, ?, {_marks(SNAPSHOT_FIELDS)})",
        [(taken_at, t.device_id, *(getattr(t, f) for f in SNAPSHOT_FIELDS)) for t in thermostats],
    )
    conn.commit()


def save_events(conn: sqlite3.Connection, events: Iterable[ThermostatEvent]) -> int:
    """How many of `events` were new. A redelivered event (same event_id) is ignored."""
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO thermostat_event VALUES (?, ?, ?, {_marks(EVENT_FIELDS)})",
        [(e.event_id, e.at, e.device_id, *(getattr(e, f) for f in EVENT_FIELDS)) for e in events],
    )
    conn.commit()
    return conn.total_changes - before


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _marks(fields: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in fields)
