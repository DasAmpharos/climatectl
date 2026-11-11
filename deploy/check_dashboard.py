"""Run every query of the dashboard through a running Grafana, in °F and in °C, and fail if
one errors or returns no rows. CI runs it against the demo data (deploy/demo_data.py), so a
change to the views that breaks a panel fails the build instead of showing an empty chart.

    GRAFANA_ADMIN_PASSWORD=... uv run python deploy/check_dashboard.py http://localhost:3000"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

DASHBOARD = Path(__file__).parent / "grafana" / "dashboards" / "house.json"
UNITS = ("fahrenheit", "celsius")


def targets(dashboard: dict) -> list[tuple[str, dict]]:
    """(where, query) for every panel target and annotation."""
    found = [(f"panel {p['title']!r} {t['refId']}", t) for p in dashboard["panels"]
             for t in p.get("targets", [])]  # fmt: skip
    found += [(f"annotation {a['name']!r}", a["target"])
              for a in dashboard["annotations"]["list"] if "target" in a]  # fmt: skip
    return found


def wait_for(url: str, seconds: int = 120) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if requests.get(f"{url}/api/health", timeout=5).ok:
                return
        except requests.RequestException:
            pass
        if time.monotonic() > deadline:
            sys.exit(f"Grafana at {url} didn't come up in {seconds} s")
        time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="e.g. http://localhost:3000")
    parser.add_argument("--days", type=float, default=2, help="time range ending now")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    auth = ("admin", os.environ["GRAFANA_ADMIN_PASSWORD"])
    wait_for(url)

    dashboard = json.loads(DASHBOARD.read_text())
    now = datetime.now(UTC)
    start, end = (int(t.timestamp() * 1000) for t in (now - timedelta(days=args.days), now))
    failures = []
    for unit in UNITS:
        for where, target in targets(dashboard):
            query = {**target, "refId": "A"}
            for key in ("rawQueryText", "queryText"):
                if key in query:
                    query[key] = (
                        query[key]
                        .replace("${unit}", unit)
                        .replace("$__from", str(start))
                        .replace("$__to", str(end))
                    )
            resp = requests.post(
                f"{url}/api/ds/query",
                auth=auth,
                json={"queries": [query], "from": str(start), "to": str(end)},
                timeout=30,
            )
            result = resp.json().get("results", {}).get("A", {}) if resp.ok else {}
            rows = sum(len((f.get("data", {}).get("values") or [[]])[0]) for f in
                       result.get("frames", []))  # fmt: skip
            if not resp.ok or result.get("error") or not rows:
                detail = result.get("error") or (resp.text[:200] if not resp.ok else "no rows")
                failures.append(f"{unit}: {where}: {detail}")
            else:
                print(f"ok  {unit:10} {where}: {rows} rows")
    if failures:
        sys.exit("FAILED\n" + "\n".join(failures))
    print("every query returned rows in both units")


if __name__ == "__main__":
    main()
