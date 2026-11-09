"""Open-Meteo weather (https://open-meteo.com): current conditions and hourly forecasts.

Open-Meteo is free for non-commercial use and needs no API key. "Current" values come from
weather models updated every 15 minutes, not from a physical station."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from . import envfile
from .envfile import ConfigError

BASE_URL = "https://api.open-meteo.com/v1/forecast"
# Open-Meteo variable -> Weather field
VARIABLES = {
    "temperature_2m": "temp_c",
    "relative_humidity_2m": "humidity_pct",
    "cloud_cover": "cloud_pct",
    "shortwave_radiation": "solar_wm2",
    "wind_speed_10m": "wind_kmh",
    "precipitation": "precip_mm",
}


class WeatherError(RuntimeError):
    pass


@dataclass(frozen=True)
class Location:
    """Home, rounded to 2 decimal places (about 1 km): plenty for weather, and it never
    pinpoints the house in a request or a log."""

    latitude: float
    longitude: float

    @classmethod
    def from_settings(cls) -> Location:
        keys = ("HOME_LATITUDE", "HOME_LONGITUDE")
        settings = envfile.read_settings(keys)
        missing = [k for k in keys if k not in settings]
        if missing:
            raise ConfigError(f"Missing in {envfile.env_path()}: {', '.join(missing)}")
        try:
            lat, lon = (round(float(settings[k]), 2) for k in keys)
        except ValueError:
            raise ConfigError("HOME_LATITUDE and HOME_LONGITUDE must be numbers.") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ConfigError("HOME_LATITUDE or HOME_LONGITUDE is out of range.")
        return cls(lat, lon)


@dataclass(frozen=True)
class Weather:
    """Conditions at one time. `at` is UTC, e.g. 2026-10-24T00:45:00Z."""

    at: str
    temp_c: float | None
    humidity_pct: float | None
    cloud_pct: float | None
    solar_wm2: float | None
    wind_kmh: float | None
    precip_mm: float | None


def current(location: Location) -> Weather:
    data = _get(location, {"current": ",".join(VARIABLES)})
    row = data["current"]
    return Weather(
        at=f"{row['time']}:00Z", **{field: row.get(var) for var, field in VARIABLES.items()}
    )


def forecast(location: Location, hours: int = 48) -> list[Weather]:
    """Hourly forecast starting with the current hour."""
    data = _get(location, {"hourly": ",".join(VARIABLES), "forecast_hours": hours})
    hourly = data["hourly"]
    return [
        Weather(
            at=f"{time}:00Z",
            **{field: hourly[var][i] for var, field in VARIABLES.items()},
        )
        for i, time in enumerate(hourly["time"])
    ]


def _get(location: Location, params: dict[str, Any]) -> dict[str, Any]:
    # Error messages must never include the request URL: it carries the coordinates.
    query = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "timezone": "GMT",
        **params,
    }
    try:
        resp = requests.get(BASE_URL, params=query, timeout=30)
    except requests.RequestException as e:
        raise WeatherError(f"Open-Meteo request failed: {type(e).__name__}") from None
    if not resp.ok:
        try:
            reason = resp.json().get("reason", "no reason given")
        except ValueError:
            reason = "unparseable error body"
        raise WeatherError(f"Open-Meteo -> {resp.status_code}: {reason}")
    return resp.json()
