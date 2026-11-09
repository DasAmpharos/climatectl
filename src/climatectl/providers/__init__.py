"""Thermostat providers: the brand-specific code behind a small interface. The logger and
`climatectl status` pick one with THERMOSTAT_PROVIDER (default nest).

A provider is a module in PROVIDERS with a `provider()` function that returns a Provider. It
must not read credentials until a method needs them, so a missing setting fails one cycle's
job, not the whole logger."""

from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Mapping
from typing import Protocol

from .. import envfile
from ..envfile import ConfigError
from ..thermostat import Thermostat

SETTING = "THERMOSTAT_PROVIDER"
DEFAULT = "nest"
PROVIDERS = {"nest": f"{__name__}.nest"}


class ProviderError(RuntimeError):
    """A provider's API failed. The logger logs it and carries on with the other jobs."""


class EventSource(Protocol):
    """Changes the provider pushes between snapshots (a Pub/Sub subscription, a webhook
    queue). Delivery may repeat an event, so it is stored by event id, and acknowledged only
    after the store commits."""

    def drain(self, conn: sqlite3.Connection, received_at: str) -> int:
        """Store every waiting event (store.save_events, plus any raw log of the provider's
        own) and return how many were new."""
        ...


class Provider(Protocol):
    name: str  # in messages, e.g. "Nest"
    # Extra advice per finding key or message, e.g. "stale:events" or "no_thermostats".
    hints: Mapping[str, str]

    def prepare(self, conn: sqlite3.Connection) -> None:
        """Create the provider's own tables and bring older rows up to date. Runs on every
        logger start, so it must be idempotent."""
        ...

    def thermostats(self) -> list[Thermostat]: ...

    def set_range(self, device_id: str, heat_c: float | None, cool_c: float | None) -> None:
        """Set the heat and cool setpoints (Celsius). Pass only one for a heat-only or
        cool-only mode."""
        ...

    def events(self) -> EventSource | None:
        """The push-event source, or None if the provider has none. Raises ConfigError when
        it has one but isn't set up for it."""
        ...


def load(name: str) -> Provider:
    if name not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS))
        raise ConfigError(f"Unknown {SETTING} {name!r}; known providers: {known}")
    return importlib.import_module(PROVIDERS[name]).provider()


def from_settings() -> Provider:
    return load(envfile.read_settings((SETTING,)).get(SETTING, DEFAULT).strip().lower())
