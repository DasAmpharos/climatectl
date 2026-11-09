import pytest
from fake_google import THERMOSTATS

from climatectl import health, logger_cli, providers, store
from climatectl.envfile import ConfigError
from climatectl.providers.nest import NestProvider, sdm
from climatectl.providers.nest.store import normalize

NOW = "2026-10-24T00:31:10Z"


def test_nest_is_the_default(env_file):
    assert isinstance(providers.from_settings(), NestProvider)


def test_provider_from_settings(env_file):
    env_file.write_text(env_file.read_text() + "THERMOSTAT_PROVIDER= Nest \n")
    assert isinstance(providers.from_settings(), NestProvider)


def test_unknown_provider(env_file):
    env_file.write_text(env_file.read_text() + "THERMOSTAT_PROVIDER=ecobee\n")
    with pytest.raises(ConfigError, match="Unknown THERMOSTAT_PROVIDER 'ecobee'.*: nest"):
        providers.from_settings()


def test_logger_runs_another_provider(fakes, authorized, tmp_path, monkeypatch, caplog):
    monkeypatch.setitem(providers.PROVIDERS, "fake", "fake_provider")
    monkeypatch.setenv("THERMOSTAT_PROVIDER", "fake")
    monkeypatch.setenv("CLIMATECTL_DB", str(tmp_path / "fake.db"))
    with caplog.at_level("INFO"):
        logger_cli.main(["--once"])
    assert "logging Fake thermostats to" in caplog.text
    assert "1 thermostat(s), 1 new event(s)" in caplog.text
    assert fakes[0].calls == []  # never asked Google
    conn = store.connect(tmp_path / "fake.db")
    assert conn.execute("SELECT device_id, status FROM hvac_status ORDER BY at").fetchall() == [
        ("hall", "heating"),
        ("hall", "heating"),
    ]
    assert conn.execute("SELECT name FROM thermostat").fetchall() == [("Hall",)]


def test_logger_with_an_unknown_provider_exits(fakes, authorized, monkeypatch):
    monkeypatch.setenv("THERMOSTAT_PROVIDER", "ecobee")
    with pytest.raises(SystemExit, match="Unknown THERMOSTAT_PROVIDER"):
        logger_cli.main(["--once"])


# --- Nest's values in the neutral vocabulary ---


def test_nest_thermostats_in_neutral_terms():
    down, up = sdm.thermostats(THERMOSTATS)
    assert (down.mode, down.hvac, down.eco) == ("heat", "heating", False)
    assert (up.mode, up.hvac, up.eco) == ("heat_cool", "idle", False)
    assert (down.eco_heat_c, down.eco_cool_c) == (16.7, 26.7)


@pytest.mark.parametrize(
    ("trait", "value", "field", "expected"),
    [
        ("ThermostatMode", {"mode": "HEAT"}, "mode", "heat"),
        ("ThermostatMode", {"mode": "COOL"}, "mode", "cool"),
        ("ThermostatMode", {"mode": "HEATCOOL"}, "mode", "heat_cool"),
        ("ThermostatMode", {"mode": "OFF"}, "mode", "off"),
        ("ThermostatMode", {"mode": "AUTO"}, "mode", "auto"),  # no neutral name: kept
        ("ThermostatHvac", {"status": "HEATING"}, "hvac", "heating"),
        ("ThermostatHvac", {"status": "COOLING"}, "hvac", "cooling"),
        ("ThermostatHvac", {"status": "OFF"}, "hvac", "idle"),
        ("ThermostatEco", {"mode": "MANUAL_ECO"}, "eco", True),
        ("ThermostatEco", {"mode": "OFF"}, "eco", False),
        ("Temperature", {"ambientTemperatureCelsius": 20.5}, "temp_c", 20.5),
        ("Humidity", {"ambientHumidityPercent": 51}, "humidity_pct", 51),
    ],
)
def test_nest_readings(trait, value, field, expected):
    readings = sdm.readings({f"sdm.devices.traits.{trait}": value})
    assert readings[field] == expected
    assert all(v is None for k, v in readings.items() if k != field)


def test_events_without_thermostat_fields_are_not_normalized():
    fan = {"sdm.devices.traits.Fan": {"timerMode": "OFF"}}
    assert normalize("e1", NOW, "up", fan) is None
    assert normalize("e2", NOW, None, {}) is None  # relationUpdate
    event = normalize("e3", NOW, "up", {"sdm.devices.traits.ThermostatHvac": {"status": "OFF"}})
    assert (event.device_id, event.hvac, event.temp_c) == ("up", "idle", None)


# --- writes ---


@pytest.mark.parametrize(
    ("heat", "cool", "command", "params"),
    [
        (19.5, 24.5, "SetRange", {"heatCelsius": 19.5, "coolCelsius": 24.5}),
        (19.5, None, "SetHeat", {"heatCelsius": 19.5}),
        (None, 24.5, "SetCool", {"coolCelsius": 24.5}),
    ],
)
def test_nest_set_range(google, authorized, heat, cool, command, params):
    NestProvider().set_range("up", heat, cool)
    prefix = "sdm.devices.commands.ThermostatTemperatureSetpoint."
    assert google.commands == [("up", prefix + command, params)]


def test_nest_set_range_errors(google, authorized):
    with pytest.raises(ValueError, match="heat or a cool"):
        NestProvider().set_range("up", None, None)
    with pytest.raises(sdm.SdmError, match="SetRange -> 404: NOT_FOUND"):
        NestProvider().set_range("gone", 19.5, 24.5)
    assert google.commands == []


def test_nest_hints_reach_the_alerts(tmp_path):
    conn = store.connect(tmp_path / "climatectl.db")
    conn.execute(
        "INSERT INTO thermostat_snapshot (taken_at, device_id, name) VALUES (?, 'up', 'Up')",
        ("2026-10-29T00:00:00Z",),
    )
    nest = NestProvider()
    findings = health.check(
        conn, store.parse_utc("2026-10-30T00:00:00Z"), True, provider=nest.name, hints=nest.hints
    )
    by_key = {f.key: f for f in findings}
    assert by_key["stale:events"].title == "Nest events stopped"
    assert by_key["stale:events"].message.startswith("No Nest events ever. Check that events")
    assert "the Nest API calls are failing" in by_key["stale:thermostats"].message
    assert by_key["stale:thermostats"].message.endswith("run `climatectl nest-auth` again.")
