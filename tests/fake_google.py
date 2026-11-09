"""In-memory stand-in for Google's OAuth token endpoint and SDM devices.list and
executeCommand."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

PROJECT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "123456789012-abcdef.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret"
CODE = "4/0Ab-test-code"
ACCESS_TOKEN = "ya29.test-access"
REFRESH_TOKEN = "1//0test-refresh"

THERMOSTATS = [
    {
        "name": f"enterprises/{PROJECT}/devices/down",
        "type": "sdm.devices.types.THERMOSTAT",
        "traits": {
            "sdm.devices.traits.Connectivity": {"status": "ONLINE"},
            "sdm.devices.traits.Temperature": {"ambientTemperatureCelsius": 21.3},
            "sdm.devices.traits.Humidity": {"ambientHumidityPercent": 41},
            "sdm.devices.traits.ThermostatHvac": {"status": "HEATING"},
            "sdm.devices.traits.ThermostatMode": {"mode": "HEAT"},
            "sdm.devices.traits.ThermostatEco": {
                "mode": "OFF",
                "heatCelsius": 16.7,
                "coolCelsius": 26.7,
            },
            "sdm.devices.traits.ThermostatTemperatureSetpoint": {"heatCelsius": 20.0},
        },
        "parentRelations": [{"parent": "rooms/1", "displayName": "Downstairs"}],
    },
    {
        "name": f"enterprises/{PROJECT}/devices/up",
        "type": "sdm.devices.types.THERMOSTAT",
        "traits": {
            "sdm.devices.traits.Info": {"customName": "Upstairs"},
            "sdm.devices.traits.Connectivity": {"status": "ONLINE"},
            "sdm.devices.traits.Temperature": {"ambientTemperatureCelsius": 22.0},
            "sdm.devices.traits.Humidity": {"ambientHumidityPercent": 38},
            "sdm.devices.traits.ThermostatHvac": {"status": "OFF"},
            "sdm.devices.traits.ThermostatMode": {"mode": "HEATCOOL"},
            "sdm.devices.traits.ThermostatEco": {"mode": "OFF"},
            "sdm.devices.traits.ThermostatTemperatureSetpoint": {
                "heatCelsius": 19.5,
                "coolCelsius": 24.5,
            },
        },
        "parentRelations": [],
    },
]
DOORBELL = {
    "name": f"enterprises/{PROJECT}/devices/door",
    "type": "sdm.devices.types.DOORBELL",
    "traits": {},
    "parentRelations": [{"parent": "rooms/2", "displayName": "Front Door"}],
}


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code, self._body = status, body
        self.ok = status < 400

    def json(self) -> dict[str, Any]:
        return self._body


class FakeGoogle:
    def __init__(self) -> None:
        self.devices = [*THERMOSTATS, DOORBELL]
        self.used_codes: set[str] = set()
        self.refresh_revoked = False
        self.calls: list[str] = []
        self.commands: list[tuple[str, str, dict[str, Any]]] = []  # device, command, params

    def post(
        self,
        url: str,
        data: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **_: Any,
    ) -> FakeResponse:
        if url.endswith(":executeCommand"):
            return self._execute(url, json or {}, headers or {})
        assert data is not None
        self.calls.append(f"POST {urlsplit(url).path} {data['grant_type']}")
        if data["client_id"] != CLIENT_ID or data["client_secret"] != CLIENT_SECRET:
            return FakeResponse(
                401, {"error": "invalid_client", "error_description": "Unauthorized"}
            )
        if data["grant_type"] == "refresh_token":
            if data["refresh_token"] != REFRESH_TOKEN or self.refresh_revoked:
                return FakeResponse(
                    400, {"error": "invalid_grant", "error_description": "Token has been revoked."}
                )
            return FakeResponse(200, {"access_token": ACCESS_TOKEN, "expires_in": 3599})
        if data["redirect_uri"] != "https://www.google.com":
            return FakeResponse(
                400, {"error": "redirect_uri_mismatch", "error_description": "Bad Request"}
            )
        if data["code"] != CODE or data["code"] in self.used_codes:
            return FakeResponse(400, {"error": "invalid_grant", "error_description": "Bad Request"})
        self.used_codes.add(data["code"])
        return FakeResponse(
            200,
            {
                "access_token": ACCESS_TOKEN,
                "expires_in": 3599,
                "refresh_token": REFRESH_TOKEN,
                "scope": "https://www.googleapis.com/auth/sdm.service",
                "token_type": "Bearer",
            },
        )

    def get(self, url: str, headers: dict[str, str], **_: Any) -> FakeResponse:
        self.calls.append(f"GET {urlsplit(url).path}")
        if headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
            return FakeResponse(
                401, {"error": {"status": "UNAUTHENTICATED", "message": "bad token"}}
            )
        return FakeResponse(200, {"devices": self.devices})

    def _execute(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
        path = urlsplit(url).path
        self.calls.append(f"POST {path}")
        if headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
            return FakeResponse(
                401, {"error": {"status": "UNAUTHENTICATED", "message": "bad token"}}
            )
        device = path.rsplit("/", 1)[-1].removesuffix(":executeCommand")
        if not any(d["name"].endswith(f"/devices/{device}") for d in self.devices):
            return FakeResponse(404, {"error": {"status": "NOT_FOUND", "message": "no device"}})
        self.commands.append((device, body["command"], body["params"]))
        return FakeResponse(200, {})
