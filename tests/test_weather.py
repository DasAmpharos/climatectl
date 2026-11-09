import pytest
from conftest import HOME_LATITUDE, HOME_LONGITUDE

from climatectl import weather
from climatectl.envfile import ConfigError


def test_location_is_rounded_to_about_a_kilometer(env_file):
    assert weather.Location.from_settings() == weather.Location(51.50, -0.12)


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ("", "Missing in .*HOME_LATITUDE, HOME_LONGITUDE"),
        ("HOME_LATITUDE=north\nHOME_LONGITUDE=1\n", "must be numbers"),
        ("HOME_LATITUDE=91\nHOME_LONGITUDE=1\n", "out of range"),
    ],
)
def test_location_errors(env_file, lines, message):
    env_file.write_text(lines)
    with pytest.raises(ConfigError, match=message):
        weather.Location.from_settings()


def test_current(meteo):
    obs = weather.current(weather.Location(51.50, -0.12))
    assert obs == weather.Weather(
        at="2026-10-24T00:45:00Z",
        temp_c=15.5,
        humidity_pct=49,
        cloud_pct=0,
        solar_wm2=0.0,
        wind_kmh=7.8,
        precip_mm=0.0,
    )
    assert (
        meteo.requests[0] | {"latitude": 51.50, "longitude": -0.12, "timezone": "GMT"}
        == (meteo.requests[0])
    )


def test_forecast(meteo):
    hours = weather.forecast(weather.Location(51.50, -0.12), hours=48)
    assert len(hours) == 48
    assert hours[0].at == "2026-10-24T00:00:00Z"
    assert hours[-1].at == "2026-10-25T23:00:00Z"
    assert [h.temp_c for h in hours[:3]] == [15.5, 16.5, 17.5]


def test_errors_never_include_coordinates(meteo, env_file):
    meteo.fail = True
    with pytest.raises(weather.WeatherError, match="400: Cannot initialize") as excinfo:
        weather.current(weather.Location.from_settings())
    for text in (HOME_LATITUDE, HOME_LONGITUDE, "51.50", "-0.12"):
        assert text not in str(excinfo.value)
