"""`climatectl scorecard`: the planner trial so far, per level and trial day.

Needs a `trial` section in planner.yaml (see planner.example.yaml). Reads the logged data only,
and saves the days in scorecard_day for dashboards. See planner/scorecard.py for the method."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from .. import store
from ..envfile import ConfigError
from . import config, scorecard


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="climatectl scorecard",
                                     description=__doc__.splitlines()[0])  # fmt: skip
    parser.add_argument("--json", action="store_true", help="print the scorecard as JSON")
    args = parser.parse_args(argv)
    try:
        cfg = config.load(config.path_from_settings())
        zone = store.home_timezone()
    except ConfigError as e:
        sys.exit(str(e))
    if cfg.trial is None:
        sys.exit("No trial in the planner config: add a `trial` section (planner.example.yaml).")
    conn = store.connect(store.db_path(), zone)
    cards = scorecard.build(conn, cfg, zone, datetime.now(UTC))
    scorecard.save(conn, cards)
    if args.json:
        print(json.dumps(scorecard.to_json(cards), indent=1))
    else:
        print(scorecard.render(cards, cfg, zone))


if __name__ == "__main__":
    main()
