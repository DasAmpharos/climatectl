import pytest
import requests
from fake_google import (
    CLIENT_ID,
    CLIENT_SECRET,
    PROJECT,
    REFRESH_TOKEN,
    FakeGoogle,
    FakeResponse,
)
from fake_meteo import URL as METEO_URL
from fake_meteo import FakeOpenMeteo

from climatectl.providers.nest.session import REQUIRED

# Unrounded on purpose: the code must round to 2 decimals before using these.
HOME_LATITUDE = "51.50072912345678"
HOME_LONGITUDE = "-0.12462612345678"


HEALTHCHECK_URL = "https://hc-ping.com/test-check-uuid"
PUSHOVER_APP_TOKEN = "app-token-for-tests"
PUSHOVER_USER_KEY = "user-key-for-tests"


@pytest.fixture
def fakes(monkeypatch) -> tuple[FakeGoogle, FakeOpenMeteo]:
    """Fake Google and Open-Meteo behind requests.post / requests.get. Heartbeat pings are
    recorded in the `pings` fixture, Pushover posts in `pushover_posts`."""
    google, meteo = FakeGoogle(), FakeOpenMeteo()
    pings: list[str] = []
    posts: list[dict] = []

    def post(url, **kwargs):
        if url.startswith("https://api.pushover.net/"):
            posts.append({"url": url, **kwargs})
            return FakeResponse(200, {})
        return google.post(url, **kwargs)

    def get(url, **kwargs):
        if url.startswith("https://hc-ping.com/"):
            pings.append(url)
            return FakeResponse(200, {})
        return (meteo if url.startswith(METEO_URL) else google).get(url, **kwargs)

    get.pings = pings
    post.posts = posts
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", get)
    return google, meteo


@pytest.fixture
def pings(fakes) -> list[str]:
    """URLs the code sent heartbeat pings to."""
    return requests.get.pings


@pytest.fixture
def pushover_posts(fakes) -> list[dict]:
    """Requests the code sent to Pushover: url and form data."""
    return requests.post.posts


@pytest.fixture
def google(fakes) -> FakeGoogle:
    return fakes[0]


@pytest.fixture
def meteo(fakes) -> FakeOpenMeteo:
    return fakes[1]


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """A throwaway .env with the auth inputs filled in, so tests never touch the real one."""
    path = tmp_path / ".env"
    path.write_text(
        "# Google\n"
        f"GOOGLE_CLIENT_ID={CLIENT_ID}\n"
        f"GOOGLE_CLIENT_SECRET={CLIENT_SECRET}\n"
        "GOOGLE_REFRESH_TOKEN=\n"
        f"SDM_PROJECT_ID={PROJECT}\n"
        f"HOME_LATITUDE={HOME_LATITUDE}\n"
        f"HOME_LONGITUDE={HOME_LONGITUDE}\n"
    )
    monkeypatch.setenv("CLIMATECTL_ENV_FILE", str(path))
    # Real environment variables would override the file.
    for var in (
        *REQUIRED,
        "HOME_LATITUDE",
        "HOME_LONGITUDE",
        "CLIMATECTL_DB",
        "CLIMATECTL_TIMEZONE",
        "THERMOSTAT_PROVIDER",
        "ALERT_FLOOR",
        "ALERT_CEILING",
        "GCP_PROJECT_ID",
        "PUBSUB_SUBSCRIPTION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "HEALTHCHECK_URL",
        "PUSHOVER_APP_TOKEN",
        "PUSHOVER_USER_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return path


@pytest.fixture
def authorized(env_file):
    """env_file after a successful `climatectl nest-auth`."""
    env_file.write_text(
        env_file.read_text().replace(
            "GOOGLE_REFRESH_TOKEN=\n", f"GOOGLE_REFRESH_TOKEN={REFRESH_TOKEN}\n"
        )
    )
    return env_file
