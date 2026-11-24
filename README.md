# climatectl

Logs your thermostats, the weather and every heating and cooling run to SQLite, and alerts
you when the HVAC looks broken. Every 15 minutes it records:
- current weather from [Open-Meteo](https://open-meteo.com), plus a 48-hour forecast every 6
  hours
- a snapshot of each thermostat
- every change the thermostats pushed since the last cycle, with its exact time

It never changes a thermostat.

Read-only SQL views then answer the usual questions: indoor temperature over time, setpoints,
how long the furnace and AC ran per day, how fast each run warmed or cooled the house, and
heating and cooling degree-days. Point Grafana (or anything that reads SQLite) at them.

After each cycle, health checks look for:
- heat that doesn't warm the house, or cooling that doesn't cool it
- short cycling
- a house too cold or too hot
- a thermostat offline
- data that stopped coming in

Each problem becomes one alert through [Pushover](https://pushover.net), with reminders while
it lasts.

Thermostats come from a **provider**. Google Nest (through Google's Smart Device Management
API) is the only one so far, and others plug in behind the same small interface (see
[Providers](#providers)). The names follow Home Assistant's climate entities, so a Home
Assistant provider would be a thin one.

This repo is only the code. Keep your own settings (`.env`) in a separate, private folder
or repo. Run climatectl there with Docker (the logger plus Grafana with the dashboard, see
[Running with Docker](#running-with-docker)), or install it with uv (see
[Setting up your own config](#setting-up-your-own-config)).

## Running with Docker

Each release publishes two images for amd64 and arm64 (a Raspberry Pi works):
`ghcr.io/dasampharos/climatectl` (the logger) and `ghcr.io/dasampharos/climatectl-grafana`
(Grafana with the SQLite plugin, the data source and the dashboard built in). Put
[`deploy/compose.yaml`](deploy/compose.yaml) and [`.env.example`](.env.example), renamed to
`.env`, in a folder of their own, fill in `.env` (see [Settings](#settings), plus
`GRAFANA_ADMIN_PASSWORD`), then:

```bash
docker compose up -d
docker compose logs -f logger    # one line per cycle: weather, thermostats, events, alerts
```

Grafana is on port 3000 (`GRAFANA_PORT` to change it): user `admin`, password
`GRAFANA_ADMIN_PASSWORD`. The dashboard has a °F/°C switch at the top. The database and
Grafana's own settings live in Docker volumes (`climatectl_data`, `climatectl_grafana`), so
they survive upgrades. To upgrade, set `CLIMATECTL_VERSION` to the new release and run
`docker compose up -d` again.

For Nest, run the one-time authorization in the same folder before starting the logger. It
needs [uv](https://docs.astral.sh/uv/), and writes the refresh token into `.env`:

```bash
uvx --from git+https://github.com/DasAmpharos/climatectl@v0.2.0 climatectl nest-auth
```

For Nest events, also mount the Pub/Sub service account key (see the comment in
`compose.yaml`).

### Trying the dashboard

`deploy/demo_data.py` writes three simulated days for two thermostats, ending now. To see
the dashboard without connecting a thermostat, from a checkout:

```bash
uv run python deploy/demo_data.py data/demo.db && chmod -R a+rwX data
docker run --rm -p 3000:3000 -v "$PWD/data:/data" -e CLIMATECTL_DB=/data/demo.db \
  -e GF_AUTH_ANONYMOUS_ENABLED=true ghcr.io/dasampharos/climatectl-grafana:0.2.0
```

Then open http://localhost:3000. The dashboard's JSON is
`deploy/grafana/dashboards/house.json`. To change it, edit a copy in Grafana, export it
(Share → Export → JSON), and replace the file. `deploy/check_dashboard.py` runs every query
against a running Grafana, which CI does on every pull request.

## Setting up your own config

To run climatectl without Docker, or to work on it, install it with uv. Requires Python 3.14
(uv installs it for you).

Make a folder (ideally a private git repo):

```
my-house/
  pyproject.toml   # depends on climatectl
  .env             # settings and secrets; never commit this
  .gitignore       # .env, .venv/, data/
```

`pyproject.toml`:

```toml
[project]
name = "my-house"
version = "0"
requires-python = ">=3.14"
dependencies = ["climatectl"]

[tool.uv]
package = false

[tool.uv.sources]
# Pin a tag or commit so updates to the shared code only reach you when you choose.
climatectl = { git = "https://github.com/DasAmpharos/climatectl", tag = "v0.2.0" }
# Or, while working on the shared code next to it:
# climatectl = { path = "../climatectl", editable = true }
```

Then:

```bash
uv sync                                # installs Python + climatectl into .venv
cp ../climatectl/.env.example .env     # then fill it in (see below)
uv run climatectl nest-auth            # one-time: saves GOOGLE_REFRESH_TOKEN to .env
uv run climatectl status               # read-only snapshot of every thermostat (--json)
uv run climatectl logger --once        # one cycle, to check everything works
uv run climatectl logger               # runs every 15 minutes until stopped
```

Commands find `.env` by searching up from the working directory, or you can point
`CLIMATECTL_ENV_FILE` at one. Real environment variables override the file, so a container
can set them too. `climatectl <command> --help` lists each command's options.

## Settings

| Setting | |
| --- | --- |
| `HOME_LATITUDE`, `HOME_LONGITUDE` | Required, for the weather. Rounded to 2 decimals (about 1 km) before use, and never logged. |
| `CLIMATECTL_TIMEZONE` | The home timezone (an IANA name such as `America/New_York`). The views count days in it. Default UTC. |
| `CLIMATECTL_DB` | The database. Default `data/climatectl.db` next to `.env`. |
| `THERMOSTAT_PROVIDER` | Default `nest`. |
| `ALERT_FLOOR`, `ALERT_CEILING` | Indoor temperatures that alert whatever the mode, with their unit. Default `55F` (a pipe-freeze margin) and `86F`. |
| `HEALTHCHECK_URL` | Optional. A [healthchecks.io](https://healthchecks.io) (or similar) URL pinged after every cycle, so you hear when the logger itself stops. |
| `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY` | Optional. Without them, alerts are only logged and kept in the `alert` table. |
| `ANTHROPIC_API_KEY` | For `climatectl plan` only, and optional: without it, the Anthropic SDK finds credentials itself, such as an [`ant`](https://github.com/anthropics/anthropic-cli) `ant auth login` profile. Never printed. |
| `PLANNER_CONFIG` | The planner's settings. Default `planner.yaml` next to `.env`. |

Treat the healthcheck URL and the Pushover values as secrets: the logger never prints them.

## Planner (dry run)

`climatectl plan` asks Claude for a 24-hour setpoint plan per thermostat: the least heating and
cooling that keeps each one inside its comfort bands. So far it only plans: each run is stored
in the `plan_run` table, and nothing is written to a thermostat.

Copy [`planner.example.yaml`](planner.example.yaml) to `planner.yaml` next to `.env` and
describe the house, each thermostat's comfort bands (a default band, plus timed ones such as
sleep) and the limits. Set `ANTHROPIC_API_KEY`, or sign in once with `ant auth login`. Then:

```bash
uv run climatectl plan --input-only   # print exactly what would be sent; free
uv run climatectl plan                # one call: prints the plan, its reasons and the cost
uv run climatectl plan --model claude-haiku-5-5 --model claude-opus-5-5
                                      # one plan each from the same input, compared
```

Each run sends:
- each thermostat's current state and bands
- its house model, fitted to your readings (`planner/house.py`): the heat loss, the furnace's
  and AC's effective rates, the sun's warming and other steady gains
- the next 24 hours of forecast, the last 7 days of runtime and degree-days
- the last 4 weeks of the optional feedback CSV, and the optional notes file

Device ids, the home location and secrets are never sent; the house description and the
feedback file are, so keep addresses out of them. About 6,000 tokens in: a few cents a run on
`claude-opus-5-5`, a fraction of a cent on `claude-haiku-5-5`.

Whatever Claude returns, guardrails in code (`planner/guardrails.py`) keep every setpoint
inside the comfort bands, the floor and ceiling, a minimum heat-cool gap, a maximum step and a
minimum interval between changes, and skip a thermostat that is offline, in Eco, off, or was
changed by hand in the last few hours. Each fix is stored as a correction, so a model that
needs them stands out. `plan_run` also keeps the input, the reply, the tokens and an estimated
cost.

Each plan is then scored by a simulator (`planner/simulate.py`): it runs the plan through the
house model against the forecast, 24 hours in 5-minute steps, and reports per thermostat the
minutes outside the comfort bands, the furnace and AC minutes, and the worst miss. Two baselines
are scored next to it: holding the current setpoints all day (no AI), and switching to each
band's edges as it starts (no pre-heating). `climatectl score [RUN_ID ...]` re-scores stored
plans. The house model is fitted to whole days of readings, so the simulator predicts how long
the furnace runs: replaying cold days held at fixed setpoints, within about 15% a day once a
level has a month of furnace runs. Its recoveries after a setback are slower than real ones,
so it pre-heats early rather than late.

### Trial and scorecard

Once plans are applied, add a `trial` to planner.yaml (see planner.example.yaml): planned days
and hold days (fixed setpoints) alternate, so both see the same weeks of weather.
`climatectl scorecard` then lists every trial day per level: its arm, outdoor temperature,
furnace and AC minutes, how long the reading was really outside the band, and the low. It
fits furnace minutes to the weather (degree-days, with the base temperature fitted) on the
days at the hold setpoints, and compares the arms through that fit: the saving a day, with a
95% interval. Before the trial it says the smallest saving the trial could detect, and it
checks the simulator's predictions against days that really held. Days are saved in
`scorecard_day` for dashboards.

`climatectl apply` runs the trial: it writes the plan's changes at their times on planned days,
and the hold setpoints when a hold day starts. It applies the first plan from the config's
`model` made when the day starts (from 15 minutes before to 30 after), so schedule
`climatectl plan` for `day_starts`; with no plan by then, the day is left as it is, you're told
(through Pushover, if set up) and the scorecard leaves it out. A change made by a person wins:
the level is left alone until the next trial day, and taken over then only once
`manual_pause_hours` have passed since the change. Offline, Eco and off skip a step. Every step
is kept in `apply_step`, with what was set or why not, so it can be restarted at any time: it
writes only the step due now. `--dry-run` says what it would set and writes nothing.

## Nest setup

The Nest provider needs a Google Device Access registration (a one-time US$5 fee) and a
Google Cloud project of your own.

1. Register at the [Device Access Console](https://console.nest.google.com/device-access)
   and create a Google Cloud project with the Smart Device Management API enabled. Put the
   Device Access project id in `SDM_PROJECT_ID` and the Cloud project id in `GCP_PROJECT_ID`.
2. Create an OAuth client (type Web application, redirect URI `https://www.google.com`), put
   its id and secret in `.env`, and publish the OAuth consent screen to **In production**. In
   Testing mode Google expires refresh tokens after 7 days.
3. For events: create a Pub/Sub topic in your Cloud project and grant
   `roles/pubsub.publisher` on it to `group:sdm-publisher@googlegroups.com`. Then set the
   topic in the Device Access Console and create a pull subscription named `sdm-events-pull`
   (or set `PUBSUB_SUBSCRIPTION`). Use `--expiration-period=never`: by default Google deletes
   a subscription that sits idle for 31 days.
4. Run `climatectl nest-auth`. It prints the Partner Connections URL, then asks for the
   `https://www.google.com/?code=...` URL Google sends you to. It saves the refresh token to
   `.env` without printing it, and lists your thermostats, which also starts the events.

Pub/Sub credentials come from Google's Application Default Credentials. On a development
machine, run `gcloud auth application-default login` once. On a server, use a service
account that can only read the subscription, and point `GOOGLE_APPLICATION_CREDENTIALS` at
its key file. Without `GCP_PROJECT_ID`, events are skipped and everything else still runs.

## Data

Every timestamp is UTC ISO 8601 ending in Z, and temperatures are Celsius.

| Table | |
| --- | --- |
| `weather_observation`, `weather_forecast` | Open-Meteo: temperature, humidity, cloud cover, solar radiation, wind, precipitation. |
| `thermostat_snapshot` | Each thermostat every 15 minutes: online, temperature, humidity, mode, HVAC status, eco, setpoints. |
| `thermostat_event` | One row per pushed change, with only the fields it changed. |
| `alert` | Every alert, from when it opened to when it cleared. |
| `sdm_event` | The Nest provider's raw log of every Pub/Sub event, as Google sent it. |

| View | |
| --- | --- |
| `thermostat` | Each device's latest name. |
| `indoor_reading`, `setpoint`, `hvac_status` | Snapshots and events merged into one series per device. |
| `hvac_run` | One row per run of the same status, with its length in minutes. |
| `hvac_cycle` | Finished heating and cooling runs, with the temperature at start and end, the outdoor temperature, and the rate. |
| `hvac_daily`, `weather_daily` | Heating and cooling minutes per local day, and outdoor temperature with degree-days (base 65°F). |
| `dew_point` | Indoor and outdoor dew point. |

Values use one vocabulary, whatever the provider:
- `mode`: `heat`, `cool`, `heat_cool` or `off`
- `hvac`: `heating`, `cooling` or `idle`. Idle means not calling for either, whatever the
  mode.
- `eco`: 0 or 1

A value with no neutral name passes through lowercased. The views are recreated on every
connect, so a new release can change them.

## Providers

A provider is a module listed in `climatectl/providers/__init__.py`, with a `provider()`
function that returns an object with:

- `name` (used in messages) and `hints`: extra advice per alert, e.g. how to re-authorize.
- `thermostats()` → a list of `climatectl.thermostat.Thermostat` in the vocabulary above.
- `set_range(device_id, heat_c, cool_c)` sets the setpoints. Nothing calls it yet: the
  planner only plans.
- `events()` → an event source whose `drain(conn, received_at)` saves the waiting changes
  with `store.save_events` and returns how many were new. Or None if the provider has none.
- `prepare(conn)` creates any tables of its own, on every logger start.

A provider must not read credentials until a method needs them, so a missing setting fails
one cycle's job, not the whole logger. `tests/fake_provider.py` is a complete in-memory
example, and `providers/nest/` is the real one.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
docker build -t climatectl:dev . && docker build -t climatectl-grafana:dev deploy/grafana
```

Tests run against in-memory fakes of Google's APIs, Open-Meteo and Pushover, and never read
a real `.env`. To run `deploy/compose.yaml` with images you built, tag them
`ghcr.io/dasampharos/climatectl:dev` and `ghcr.io/dasampharos/climatectl-grafana:dev` and set
`CLIMATECTL_VERSION=dev`. See [CONTRIBUTING.md](CONTRIBUTING.md) for pull requests and new
providers.

## Dependency updates

Renovate keeps `uv.lock`, the Docker base images and the GitHub Actions pins current, with **no pull requests**: it runs daily from
`.github/workflows/renovate.yml`, pushes each update (once the release is a week old) to a `renovate/...`
branch, and merges it into `main` itself after CI passes there. An update that fails CI stays on its branch and
shows on the Dependency Dashboard issue. Settings are in `renovate.json5`.

A new Python in the images (say 3.14 to 3.15) opens a pull request instead. Merged updates ship with the
next release tag.

One-time setup: the workflow needs the repository secret `RENOVATE_TOKEN`, a fine-grained personal access
token (github.com/settings/personal-access-tokens) with access to this repository and **Contents**,
**Pull requests**, **Issues**, **Commit statuses** and **Workflows** set to *Read and write*. One token
can serve every repository that runs Renovate (homelab, gmail-cleanup and the others): give it access to
this one, then `gh secret set RENOVATE_TOKEN --repo DasAmpharos/climatectl` and run the workflow once from the
Actions tab. The token expires (at most a year): when the workflow starts failing, make a new one and set it
again.

## Releasing

The version comes from the git tag, so there is no version number to edit. Once the changes
are merged to `main`:

```bash
git switch main && git pull
git tag -a v0.2.0 -m "v0.2.0"   # semver: bump the minor for features, the patch for fixes
git push origin v0.2.0
```

The tag also publishes both Docker images (`.github/workflows/release.yml`). Docker users
move to it with `CLIMATECTL_VERSION`, and config repos by changing `tag = "v0.2.0"` and
running `uv lock`. Update the default `CLIMATECTL_VERSION` in `deploy/compose.yaml` and the
version in this README's examples in the release's pull request.
`climatectl --version` shows what is installed. Between tags, builds get a development
version such as `0.2.1.dev2+g1a2b3c4`.
