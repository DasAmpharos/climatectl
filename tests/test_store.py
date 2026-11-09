from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from climatectl import store
from climatectl.envfile import ConfigError


def test_db_path_next_to_the_env_file(env_file):
    assert store.db_path() == env_file.parent / "data" / "climatectl.db"


def test_db_path_from_the_environment(env_file, monkeypatch):
    monkeypatch.setenv("CLIMATECTL_DB", "/srv/climate.db")
    assert store.db_path() == Path("/srv/climate.db")


def test_home_timezone(env_file, monkeypatch):
    assert store.home_timezone() == ZoneInfo("UTC")
    monkeypatch.setenv("CLIMATECTL_TIMEZONE", "America/New_York")
    assert store.home_timezone() == ZoneInfo("America/New_York")
    monkeypatch.delenv("CLIMATECTL_TIMEZONE")
    env_file.write_text(env_file.read_text() + "CLIMATECTL_TIMEZONE=Europe/Berlin\n")
    assert store.home_timezone() == ZoneInfo("Europe/Berlin")


@pytest.mark.parametrize("name", ["Mars/Olympus_Mons", "../etc/passwd"])
def test_home_timezone_must_exist(env_file, monkeypatch, name):
    monkeypatch.setenv("CLIMATECTL_TIMEZONE", name)
    with pytest.raises(ConfigError, match="Not an IANA timezone"):
        store.home_timezone()
