import json

import pytest
from fake_google import ACCESS_TOKEN, CLIENT_SECRET, PROJECT, REFRESH_TOKEN, THERMOSTATS

from climatectl import status_cli
from climatectl.providers.nest import sdm


def test_status_shows_each_thermostat(google, authorized, capsys):
    status_cli.main([])
    out = capsys.readouterr().out
    assert out == (
        "Downstairs\n"
        "  Indoor   70°F (21.3°C), 41% humidity\n"
        "  System   heat mode, heating now\n"
        "  Target   heat to 68°F (20.0°C)\n"
        "\n"
        "Upstairs\n"
        "  Indoor   72°F (22.0°C), 38% humidity\n"
        "  System   heat_cool mode, idle\n"
        "  Target   heat to 67°F (19.5°C), cool to 76°F (24.5°C)\n"
    )
    assert "Front Door" not in out  # not a thermostat
    assert google.calls == [
        "POST /oauth2/v4/token refresh_token",
        f"GET /v1/enterprises/{PROJECT}/devices",
    ]


def test_status_json(google, authorized, capsys):
    status_cli.main(["--json"])
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["Downstairs", "Upstairs"]
    assert rows[0] | {"device_id": "down", "heat_c": 20.0, "humidity_pct": 41} == rows[0]


def test_never_prints_secrets(google, authorized, capsys):
    status_cli.main([])
    status_cli.main(["--json"])
    captured = capsys.readouterr()
    for secret in (CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in captured.out + captured.err


def test_needs_nest_auth_first(google, env_file):
    with pytest.raises(SystemExit, match="Run `climatectl nest-auth` first"):
        status_cli.main([])


def test_revoked_refresh_token(google, authorized):
    google.refresh_revoked = True
    with pytest.raises(SystemExit, match="invalid_grant") as excinfo:
        status_cli.main([])
    assert "Run `climatectl nest-auth` again" in str(excinfo.value)


def test_environment_overrides_env_file(google, authorized, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "1//0wrong")
    with pytest.raises(SystemExit, match="invalid_grant"):
        status_cli.main([])


def test_eco_and_offline():
    device = {
        **THERMOSTATS[0],
        "traits": {
            **THERMOSTATS[0]["traits"],
            "sdm.devices.traits.Connectivity": {"status": "OFFLINE"},
            "sdm.devices.traits.ThermostatEco": {
                "mode": "MANUAL_ECO",
                "heatCelsius": 16.7,
                "coolCelsius": 26.7,
            },
        },
    }
    text = status_cli.render(sdm.thermostat(device))
    assert text.splitlines()[0] == "Downstairs (OFFLINE: readings may be stale)"
    assert "Eco: heat 62°F (16.7°C), cool 80°F (26.7°C) (normal setpoints paused)" in text


def test_missing_traits_render_as_unknown():
    bare = {"name": "enterprises/p/devices/x", "type": sdm.THERMOSTAT_TYPE, "traits": {}}
    t = sdm.thermostat(bare)
    assert t.online is None and t.ambient_c is None
    assert status_cli.render(t) == (
        "(unnamed)\n  Indoor   ?\n  System   ? mode, ?\n  Target   none (system off)"
    )
