# Contributing

Pull requests are welcome. CI runs on every one, and checks:
- lint, formatting and the tests
- the logger image on amd64 and arm64
- every dashboard query through Grafana against the demo data

To run the same checks locally:

```bash
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pytest
uv run python deploy/demo_data.py data/demo.db   # then see "Trying the dashboard" in the README
```

Conventions: Python 3.14, ruff with line length 100, and type hints. Comments say why, not
what. Tests never touch the network: each outside API gets an in-memory fake in `tests/`,
like `fake_google.py` and `fake_meteo.py`.

## Adding a provider

A provider connects one kind of thermostat. The README's [Providers](README.md#providers)
section describes the interface. `tests/fake_provider.py` is a minimal complete one, and
`src/climatectl/providers/nest/` is the real Nest provider. A new one needs:

1. **A package** at `src/climatectl/providers/<name>/` with a `provider()` function, and an
   entry in `PROVIDERS` in `src/climatectl/providers/__init__.py`.
2. **The neutral vocabulary** (`src/climatectl/thermostat.py`):
   - mode: `heat`, `cool`, `heat_cool` or `off`
   - hvac: `heating`, `cooling` or `idle`
   - eco: a boolean
   - temperatures in Celsius
   - device ids that stay the same over time, since the history is keyed on them

   Pass through values with no neutral name, lowercased. The views and health checks only
   act on `heating` and `cooling`.
3. **Errors** that subclass `ProviderError`, with messages that never include a token or a
   URL holding one. Credentials are read only when a method needs them, never at import.
   A missing setting raises `envfile.missing([...])`.
4. **Events**, if the system can report changes between snapshots: `events()` returns an
   object whose `drain(conn, received_at)` saves `ThermostatEvent`s with `store.save_events`.
   Give each event an id that stays the same if it's fetched again. Return `None` if there
   is no event source.
5. **Tests** against a fake of the API:
   - the vocabulary mapping
   - a missing setting and an API error
   - a logger cycle with `THERMOSTAT_PROVIDER=<name>` (see `tests/test_providers.py`)
6. **Docs**: the provider's settings in `.env.example` and a setup section in the README.

### Notes for a Home Assistant provider

These are starting points to check against your own Home Assistant, not a spec:
- **Reading thermostats.** The REST API (`GET /api/states`, with a long-lived access token as
  a Bearer header) lists `climate.*` entities:
  - The state is the HVAC mode (`heat`, `cool`, `heat_cool`, `auto`, `off`, ...), already
    in this vocabulary.
  - The attributes include `current_temperature`, `current_humidity`, `hvac_action`,
    `temperature` (single target), `target_temp_low` and `target_temp_high`, and
    `preset_mode`.
  - A state of `unavailable` means offline.
- **Units.** Temperatures come in Home Assistant's unit system (`GET /api/config`,
  `unit_system.temperature`), so convert °F to Celsius.
- **HVAC action.** `hvac_action` uses Home Assistant's names, which match this vocabulary
  except `off`. Mapping `off` to `idle` keeps the HVAC charts consistent across providers.
- **Eco.** Eco is usually `preset_mode == "eco"`, but presets vary by brand.
- **Events.** `GET /api/history/period/<since>?filter_entity_id=...` returns each entity's
  changes since a time. That suits `drain()`, which runs every 15 minutes:
  - Fetch since the newest stored event for those devices.
  - Use `<entity_id>@<last_changed>` as the event id.
  - Make sure attribute-only changes (an `hvac_action` change) come back
    (`significant_changes_only=0`).
- **Device ids.** The `entity_id` is stable unless someone renames the entity.
- **Setting temperatures.** `POST /api/services/climate/set_temperature` takes
  `target_temp_low` and `target_temp_high` in `heat_cool` mode, or `temperature` for heat
  or cool. Values are in Home Assistant's unit.
