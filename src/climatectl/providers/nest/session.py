"""Credentials from .env (or the environment) turned into a ready-to-use access token."""

from __future__ import annotations

from dataclasses import dataclass, field

from ... import envfile
from ...envfile import ConfigError
from . import oauth

__all__ = ["REQUIRED", "ConfigError", "Session", "load_settings", "open_session"]

REQUIRED = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "SDM_PROJECT_ID")


@dataclass(frozen=True)
class Session:
    project_id: str
    access_token: str = field(repr=False)


def load_settings() -> dict[str, str]:
    settings = envfile.read_settings(REQUIRED)
    missing = [k for k in REQUIRED if k not in settings]
    if missing == ["GOOGLE_REFRESH_TOKEN"]:
        raise ConfigError("No GOOGLE_REFRESH_TOKEN yet. Run `climatectl nest-auth` first.")
    if missing:
        raise envfile.missing(missing)
    return settings


def open_session() -> Session:
    settings = load_settings()
    token = oauth.refresh_access_token(
        settings["GOOGLE_CLIENT_ID"],
        settings["GOOGLE_CLIENT_SECRET"],
        settings["GOOGLE_REFRESH_TOKEN"],
    )
    return Session(project_id=settings["SDM_PROJECT_ID"], access_token=token)
