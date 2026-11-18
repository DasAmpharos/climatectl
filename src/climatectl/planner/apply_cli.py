"""`climatectl apply`: writes the trial's setpoints to the thermostats (see planner/apply.py).

Needs a `trial` in planner.yaml. On planned days it applies the plan from the config's `model`
made when the day starts, so something must run `climatectl plan` then; on hold days it sets
the hold setpoints. A change made by a person is left alone for the rest of the trial day.
Every step, and every reason one wasn't written, is kept in apply_step.

Checks every minute; the thermostats are only read when a step is due. With Pushover set up
(PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY) it says when a day can't follow its arm. No secret
is ever logged."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime, timedelta

from .. import alerts, envfile, providers, store
from ..envfile import ConfigError
from . import apply, config, runs

log = logging.getLogger("climatectl")

INTERVAL = timedelta(minutes=1)
PUSHOVER_SETTINGS = ("PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="climatectl apply", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="say what would be set; write nothing to the thermostats or the database",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = config.load(config.path_from_settings())
        zone = store.home_timezone()
        provider = providers.from_settings()
    except ConfigError as e:
        sys.exit(str(e))
    if cfg.trial is None:
        sys.exit("No trial in the planner config: add a `trial` section (planner.example.yaml).")
    settings = envfile.read_settings(PUSHOVER_SETTINGS)
    notify = None
    if all(k in settings for k in PUSHOVER_SETTINGS):
        send = alerts.pushover(*(settings[k] for k in PUSHOVER_SETTINGS))

        def notify(title: str, message: str) -> None:
            send(title, message, "default")

    conn = store.connect(store.db_path(), zone)
    runs.prepare(conn)
    apply.prepare(conn)
    applier = apply.Applier(conn, cfg, provider, zone, write=not args.dry_run, notify=notify)
    t = cfg.trial
    log.info(
        "applying the trial %s-%s (days from %s; planned days from %s)%s",
        t.start, t.end, t.day_starts.strftime("%H:%M"), cfg.model,
        " as a dry run" if args.dry_run else "",
    )  # fmt: skip
    while True:
        for line in applier.tick(datetime.now(UTC)):
            log.info("%s", line)
        if args.once:
            return
        now = datetime.now(UTC)
        wake = now.replace(second=5, microsecond=0) + INTERVAL
        time.sleep(max(1.0, (wake - datetime.now(UTC)).total_seconds()))


if __name__ == "__main__":
    main()
