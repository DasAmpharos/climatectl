"""In-memory stand-in for Open-Meteo's /v1/forecast."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fake_google import FakeResponse

URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_TIME = "2026-10-24T00:45"
CURRENT = {
    "temperature_2m": 15.5,
    "relative_humidity_2m": 49,
    "cloud_cover": 0,
    "shortwave_radiation": 0.0,
    "wind_speed_10m": 7.8,
    "precipitation": 0.0,
}


class FakeOpenMeteo:
    def __init__(self) -> None:
        self.current_time = CURRENT_TIME
        self.fail = False
        self.requests: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any], **_: Any) -> FakeResponse:
        self.requests.append(params)
        if self.fail:
            return FakeResponse(400, {"error": True, "reason": "Cannot initialize WeatherVariable"})
        if "current" in params:
            row = {"time": self.current_time, "interval": 900}
            row.update({var: CURRENT[var] for var in params["current"].split(",")})
            return FakeResponse(200, {"current": row})
        start = datetime.fromisoformat(self.current_time).replace(minute=0)
        times = [
            (start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M")
            for h in range(params["forecast_hours"])
        ]
        hourly: dict[str, list[Any]] = {"time": times}
        for var in params["hourly"].split(","):
            hourly[var] = [
                CURRENT[var] + (h if var == "temperature_2m" else 0) for h in range(len(times))
            ]
        return FakeResponse(200, {"hourly": hourly})
